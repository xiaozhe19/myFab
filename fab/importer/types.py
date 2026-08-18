"""SMT2020 原始工作簿的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


RawCell = str | int | float | bool | datetime | None


@dataclass(frozen=True)
class RawSheetData:
    """一张 SMT 表的表头与原始数据行。"""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[RawCell, ...], ...]


@dataclass(frozen=True)
class RawSmtWorkbook:
    """可直接写入 SQLite 的 SMT 工作簿原始数据。"""

    source_path: Path
    sheets: tuple[RawSheetData, ...]
