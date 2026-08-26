"""SMT2020 Excel 到 SQLite 模型库的命令行入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from fab.importer import Importer, write_raw_smt_workbook
from fab.model.entities import SimulationSpec


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODEL_DATABASE = PROJECT_ROOT / "data" / "model" / "fab_model.sqlite"


def main() -> None:
    parser = argparse.ArgumentParser(description="Import one SMT2020 workbook into a raw SQLite database.")
    parser.add_argument("--input", type=Path, required=True, help="SMT2020 .xlsx 文件")
    parser.add_argument("--database", type=Path, default=DEFAULT_MODEL_DATABASE, help="目标 SQLite 原始数据文件")
    parser.add_argument("--end-time", type=float, required=True, help="仿真结束时刻（minute）")
    parser.add_argument(
        "--release-interval",
        type=float,
        default=120.0,
        help="策略投料决策间隔（minute，默认 120）",
    )
    parser.add_argument("--start-time", type=float, default=0.0, help="仿真开始时刻（minute）")
    parser.add_argument("--warmup-time", type=float, default=0.0, help="warm-up 时长（minute）")
    parser.add_argument("--replace", action="store_true", help="显式重建已有模型库")
    args = parser.parse_args()

    simulation = SimulationSpec(args.start_time, args.end_time, args.release_interval, args.warmup_time)
    data = Importer(args.input).read()
    write_raw_smt_workbook(
        data, args.database, simulation, replace=args.replace,
    )
    print(
        f"Imported {args.input.name}: {len(data.sheets)} raw sheets into {args.database}."
    )


if __name__ == "__main__":
    main()
