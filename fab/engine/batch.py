"""处理支持批量加工的机器逻辑"""

from __future__ import annotations

import re

from fab.model.entities import LotState, FabModel, ToolGroupSpec, ToolState

class BatchRules:
    """两个lot是否可以同炉 一个lot算多少组批数量"""
    def __init__(self, model: FabModel):
        self.model = model
        #机器id -> 机器组
        self._group_by_tool_id = {
            tool_id : model.tool_groups.get(spec.tool_group_id or "")
            for tool_id, spec in model.tools.items()
        }
        #工具组id -> 组批条件
        self._citeria_by_group: dict[str, dict[str,bool]] = {}



