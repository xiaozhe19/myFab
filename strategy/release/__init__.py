"""投料员（ReleasePolicy）实现，与派工规则任意组合。"""

from strategy.release.fixed_interval import FixedIntervalRelease
from strategy.release.push import PushRelease

__all__ = ["FixedIntervalRelease", "PushRelease"]
