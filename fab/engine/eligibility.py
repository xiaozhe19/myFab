"""引擎共享的设备状态检查。"""

from __future__ import annotations

from fab.model.entities import ToolState


def tool_is_available(tool: ToolState, current_time: float) -> bool:
    """检查设备是否未加工、未故障、未维护/换型且已经可用。"""

    return (
        not tool.is_down
        and not tool.is_in_setup
        and tool.active_count < tool.capacity
        and tool.available_time <= current_time
    )
