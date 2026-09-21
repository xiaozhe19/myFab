"""仿真中分布时长的统一采样逻辑。"""

from __future__ import annotations

import random

from fab.model.entities import DistributionSpec


def sample_duration(
    rng: random.Random,
    distribution: DistributionSpec | None,
    fallback: float = 0.0,
) -> float:
    """采样一次 SMT 分布时长；未给分布时返回 fallback。"""

    return sample_duration_sum(rng, distribution, fallback, 1)


def sample_duration_sum(
    rng: random.Random,
    distribution: DistributionSpec | None,
    fallback: float,
    count: int,
) -> float:
    """返回 count 个独立时长之和，只解析一次分布参数。"""

    if count <= 0:
        return 0.0
    if distribution is None:
        return fallback * count
    kind = distribution.kind.lower()
    if kind in {"constant", "deterministic"}:
        return distribution.mean * count
    if kind == "uniform":
        low = max(distribution.mean - distribution.offset, 0.0)
        high = distribution.mean + distribution.offset
        return sum(rng.uniform(low, high) for _ in range(count))
    if kind == "exponential":
        rate = 1 / distribution.mean
        return sum(rng.expovariate(rate) for _ in range(count))
    raise ValueError(f"不支持的分布：{distribution.kind}。")
