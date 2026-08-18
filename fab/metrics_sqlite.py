"""把一次仿真结果追加到 SQLite。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY, strategy TEXT,
    elapsed_seconds REAL
);
CREATE TABLE IF NOT EXISTS operations (
    run_id INTEGER, lot_id TEXT, product_id TEXT, tool_id TEXT, process TEXT,
    step INTEGER, start_time REAL, end_time REAL, duration REAL
);
CREATE TABLE IF NOT EXISTS lots (
    run_id INTEGER, lot_id TEXT, product_id TEXT, release_time REAL,
    end_time REAL, completed INTEGER, step INTEGER
);
CREATE TABLE IF NOT EXISTS metrics (run_id INTEGER, name TEXT, value REAL);
CREATE TABLE IF NOT EXISTS cqt_violations (run_id INTEGER, lot_id TEXT, target_step INTEGER, time REAL);
CREATE TABLE IF NOT EXISTS decision_log (
    run_id INTEGER, seq INTEGER, time REAL, kind TEXT, diagnostics TEXT
);
"""


def save_simulation_result(result: dict[str, object], database_path: Path) -> int:
    """保存一次结果并返回自增 run_id。"""

    if result.get("time_unit") != "minute":
        raise ValueError("结果必须使用 minute。")
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)
        # 兼容旧库：runs 表若缺少 elapsed_seconds 列则补上。
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "elapsed_seconds" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN elapsed_seconds REAL")
        cursor = connection.execute(
            "INSERT INTO runs(strategy, elapsed_seconds) VALUES (?, ?)",
            (result["strategy"], result.get("elapsed_seconds")),
        )
        run_id = cursor.lastrowid
        if run_id is None:
            raise RuntimeError("无法创建 run_id。")
        connection.executemany(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    row["lot_id"],
                    row["product_id"],
                    row["tool_id"],
                    row["process"],
                    row["step"],
                    row["start"],
                    row["end"],
                    row["duration"],
                )
                for row in result["operations"]
            ],  # type: ignore[index]
        )
        connection.executemany(
            "INSERT INTO lots VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    row["id"],
                    row["product_id"],
                    row["release_time"],
                    row["end_time"],
                    int(row["completed"]),
                    row["step"],
                )
                for row in result["lots"]
            ],  # type: ignore[index]
        )
        connection.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?)",
            [(run_id, name, value) for name, value in result["measurement"].items()],  # type: ignore[index,union-attr]
        )
        connection.executemany(
            "INSERT INTO cqt_violations VALUES (?, ?, ?, ?)",
            [
                (run_id, row["lot_id"], row["target_step"], row["time"])
                for row in result.get("cqt_violations", [])
            ],  # type: ignore[union-attr]
        )
        connection.executemany(
            "INSERT INTO decision_log VALUES (?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    seq,
                    row["time"],
                    row["kind"],
                    json.dumps(row["diagnostics"], ensure_ascii=False, default=str),
                )
                for seq, row in enumerate(result.get("decision_log", []))
            ],
        )
    return run_id
