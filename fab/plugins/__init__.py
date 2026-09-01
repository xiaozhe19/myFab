"""FabSim 的可选观察与结果收集插件。"""

from fab.plugins.base import FabPlugin, PluginResultContext
from fab.plugins.plugin_manager import PluginManager
from fab.plugins.trace import (
    CqtViolationPlugin,
    DecisionLogPlugin,
    TracePlugin,
)
from fab.plugins.results import (
    FinalStateSnapshotPlugin,
    OnlineMetricsPlugin
)

__all__ = [
    "CqtViolationPlugin",
    "DecisionLogPlugin",
    "FabPlugin",
    "TracePlugin",
    "PluginManager",
    "PluginResultContext",
    "FinalStateSnapshotPlugin",
    "OnlineMetricsPlugin"
]
