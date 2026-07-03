from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_machine_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("Machine config JSON must contain an object.")
    return payload
