"""Tool Group Token V2 快照解码。

存储格式（fab/plugins/tokenizer_core.py）是两张表：
- 工步表 step_counts [工步数, 阶段数]：每个路线工步上各阶段的 lot 数（uint16）；
- 组表 group_features [组数, 19]：C(4) M(4) B(4) policy(7)。

组级 W、Q、组内格子等由 ``fabotron.layout.TokenLayout`` 按"工步 -> 组"现算。
"""

from __future__ import annotations

import numpy as np

from fab.plugins.tokenizer_core import unpack_array


def decode_snapshot(snapshot: dict, schema: dict) -> tuple[np.ndarray, np.ndarray]:
    """一条快照记录 -> (step_counts uint16 [S, K], group_features float32 [G, F])。"""

    steps, stages = schema["step_counts"]["shape"]
    groups, features = schema["group_features"]["shape"]
    counts = np.frombuffer(unpack_array(snapshot["step_counts"], "H", steps * stages), dtype=np.uint16)
    values = np.frombuffer(unpack_array(snapshot["group_features"], "f", groups * features), dtype=np.float32)
    return counts.reshape(steps, stages), values.reshape(groups, features)
