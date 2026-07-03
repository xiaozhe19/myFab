from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def merge_machine_config(
    factory_config: dict[str, Any],
    machine_config: dict[str, Any],
) -> dict[str, Any]:
    config = deepcopy(factory_config)
    config["machines"] = deepcopy(machine_config.get("machines", []))
    config["machine_downtime"] = deepcopy(
        machine_config.get("machine_downtime", {})
    )
    return config


def merge_product_config(
    factory_config: dict[str, Any],
    product_config: dict[str, Any],
) -> dict[str, Any]:
    config = deepcopy(factory_config)
    config["products"] = deepcopy(product_config.get("products", []))
    return config


def build_factory_config(
    factory_config: dict[str, Any],
    product_config: dict[str, Any],
    machine_config: dict[str, Any],
) -> dict[str, Any]:
    return merge_machine_config(
        merge_product_config(factory_config, product_config),
        machine_config,
    )


def load_seed_sets(path: Path) -> list[dict[str, int]]:
    payload = load_json(path)
    seed_sets = payload.get("seed_sets") if isinstance(payload, dict) else payload
    if not isinstance(seed_sets, list) or not seed_sets:
        raise ValueError(f"{path} must contain a non-empty seed_sets list.")

    normalized: list[dict[str, int]] = []
    for index, item in enumerate(seed_sets, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Seed set #{index} must be a JSON object.")
        try:
            normalized.append(
                {
                    "order_seed": int(item["order_seed"]),
                    "downtime_seed": int(item["downtime_seed"]),
                    "setup_seed": int(item["setup_seed"]),
                }
            )
        except KeyError as error:
            raise ValueError(
                f"Seed set #{index} is missing {error.args[0]}."
            ) from error
    return normalized


def load_seed_set(path: Path, seed_index: int) -> dict[str, int]:
    seed_sets = load_seed_sets(path)
    if seed_index < 1 or seed_index > len(seed_sets):
        raise ValueError(f"--seed-index must be between 1 and {len(seed_sets)}.")
    return seed_sets[seed_index - 1]


def apply_factory_seeds(
    factory_config: dict[str, Any],
    seed_set: dict[str, int],
) -> dict[str, Any]:
    config = deepcopy(factory_config)
    config.setdefault("machine_downtime", {})["random_seed"] = int(
        seed_set["downtime_seed"]
    )
    config.setdefault("setup", {})["random_seed"] = int(seed_set["setup_seed"])
    return config
