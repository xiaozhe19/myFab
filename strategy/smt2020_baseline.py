"""SMT2020 默认规则（HVLM 数据集的基准）= 公共底座 + FIFO 排序键。

行为全部来自 ``BaseRule`` 与 ``FIFORule``，这里只保留既有名称：CLI 的
``--strategy SMT2020``、测试与历史结果都使用 ``SMT2020_BASELINE``。
"""

from __future__ import annotations

from fab.strategy import ReleasePolicy
from strategy.fifo import FIFORule


class SMT2020BaselineStrategy(FIFORule):
    name = "SMT2020_BASELINE"

    def __init__(
        self,
        setup_aware_ranking: bool = False,
        release: ReleasePolicy | None = None,
    ) -> None:
        super().__init__(setup_aware_ranking, release)
        if setup_aware_ranking:
            # 沿用 2026-10-26 引入该开关时的结果名称。
            self.name = "SMT2020_SETUP_AWARE"
