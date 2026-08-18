"""从原始 SMT SQLite 库构造一次运行用的 ``FabModel``。"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from fab.importer.types import RawSheetData, RawSmtWorkbook
from fab.model.entities import FabModel, SimulationSpec
from fab.model.smt_model_builder import build_fab_model


def load_fab_model_from_sqlite(database_path: Path) -> FabModel:
    """读取原始 SMT 表，在内存中构造 ``FabModel``，不向数据库写回派生数据。"""

    database_path = Path(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        simulation = _simulation(connection)
        workbook = _raw_workbook(connection, database_path)
    return build_fab_model(workbook, simulation, model_id=database_path.stem)


def _simulation(connection: sqlite3.Connection) -> SimulationSpec:
    row = connection.execute("SELECT * FROM simulation_config").fetchone()
    if row is None:
        raise ValueError("模型库缺少 simulation_config。")
    return SimulationSpec(row["start_time"], row["end_time"], row["release_interval"], row["warmup_time"])


def _raw_workbook(connection: sqlite3.Connection, database_path: Path) -> RawSmtWorkbook:
    sheets = []
    for sheet in connection.execute("SELECT sheet_name, headers_json FROM smt_sheets ORDER BY rowid"):
        rows = tuple(
            tuple(json.loads(row["values_json"]))
            for row in connection.execute(
                "SELECT values_json FROM smt_rows WHERE sheet_name = ? ORDER BY row_index",
                (sheet["sheet_name"],),
            )
        )
        sheets.append(RawSheetData(
            sheet["sheet_name"],
            tuple(json.loads(sheet["headers_json"])),
            rows,
        ))
    if not sheets:
        raise ValueError("模型库不含原始 SMT 表；请重新导入工作簿。")
    return RawSmtWorkbook(database_path, tuple(sheets))
