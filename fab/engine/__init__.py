"""新事件引擎的基础组件。

本阶段只提供状态、事件日历和资格判断。策略适配将在后续迁移时接入。
"""

from fab.engine.engine import FabEngine
from fab.engine.eligibility import eligible_lots, eligible_tools, tool_can_process

__all__ = ["FabEngine", "eligible_lots", "eligible_tools", "tool_can_process"]
