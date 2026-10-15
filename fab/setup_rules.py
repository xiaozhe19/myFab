"""SMT2020 Setups 表的查表规则，供引擎和策略共用。"""

from __future__ import annotations

from fab.model.entities import (
    DistributionSpec,
    FabModel,
    RouteStepSpec,
    SetupTransitionSpec,
)


class SetupRules:
    """按 (当前 setup, 新 setup) 查找切换规则。

    Setups 表中 CURRENT SETUP 为空的行（如 Implant_Gas）表示"从任意 setup
    切换到新 setup"，精确匹配失败时退回到该通配行。
    """

    def __init__(self, model: FabModel) -> None:
        self._transitions: dict[tuple[str, str], SetupTransitionSpec] = {}
        for transition in model.setup_transitions:
            self._transitions.setdefault(
                (transition.current_setup, transition.new_setup), transition
            )

    def transition(
        self, current: str | None, new: str | None
    ) -> SetupTransitionSpec | None:
        if not new:
            return None
        exact = self._transitions.get((current or "", new))
        if exact is not None:
            return exact
        return self._transitions.get(("", new))

    def distribution(
        self, current: str | None, step: RouteStepSpec
    ) -> DistributionSpec | None:
        """需要 setup 时使用的时长分布；Setups 表优先，其次是路线工步自带分布。"""

        transition = self.transition(current, step.required_setup)
        return transition.setup_time if transition else step.setup_distribution

    def needs_setup(self, current: str | None, step: RouteStepSpec) -> bool:
        return bool(step.required_setup) and step.required_setup != current

    def expected_time(self, current: str | None, step: RouteStepSpec) -> float:
        """期望 setup 时长（minute）；无需 setup 时为 0。"""

        if not self.needs_setup(current, step):
            return 0.0
        distribution = self.distribution(current, step)
        return distribution.mean if distribution is not None else 0.0

    def min_run(self, current: str | None, new: str | None) -> int:
        """切换到 new 之前，current setup 至少需要连续加工的 lot 数。"""

        if not current or not new or current == new:
            return 0
        transition = self.transition(current, new)
        return transition.minimal_run_length if transition else 0
