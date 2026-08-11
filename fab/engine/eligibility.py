"""派工前的资格检查，不包含任何调度优先级或选择规则。"""

from __future__ import annotations

from fab.model.entities import FabModel, LotState, RouteStepSpec, ToolState


def tool_can_process(model: FabModel, tool_id: str, lot: LotState) -> bool:
    """检查静态工具组资格及 lot-to-lens dedication。"""

    step = lot.current_step
    tool = model.tools.get(tool_id)
    if step is None or tool is None:
        return False
    if step.tool_group_id is None or tool.tool_group_id is None:
        return False
    if step.tool_group_id != tool.tool_group_id:
        return False
    if step.process != tool.process:
        return False
    return lot.dedicated_tool_id in {None, tool_id}


def tool_is_available(tool: ToolState, current_time: float) -> bool:
    """检查设备是否未加工、未故障、未维护/换型且已经可用。"""

    return (
        not tool.in_process
        and not tool.is_down
        and not tool.is_in_setup
        and tool.available_time <= current_time
    )


def setup_change_allowed(
    model: FabModel, tool: ToolState, step: RouteStepSpec
) -> bool:
    """执行 setup minimum run length 这一硬约束。"""

    required_setup = step.required_setup
    if required_setup is None or required_setup == tool.current_setup:
        return True
    minimum_run = max(
        (
            transition.minimal_run_length
            for transition in model.setup_transitions
            if transition.current_setup == (tool.current_setup or "")
            and transition.new_setup == required_setup
        ),
        default=0,
    )
    return tool.current_setup_run_length >= minimum_run


def eligible_lots(
    model: FabModel,
    tool: ToolState,
    lots: list[LotState],
    current_time: float,
) -> list[LotState]:
    """返回当前设备可加工的未排序 Lot 集合。"""

    if not tool_is_available(tool, current_time):
        return []
    return [
        lot
        for lot in lots
        if not lot.in_process
        and not lot.completed
        and lot.ready_time <= current_time
        and tool_can_process(model, tool.tool_id, lot)
        and lot.current_step is not None
        and setup_change_allowed(model, tool, lot.current_step)
    ]


def eligible_tools(
    model: FabModel,
    tool_states: dict[str, ToolState],
    lot: LotState,
    current_time: float,
) -> list[ToolState]:
    """返回可加工指定 Lot 的未排序设备集合。"""

    return [
        tool
        for tool in tool_states.values()
        if tool_is_available(tool, current_time)
        and tool_can_process(model, tool.tool_id, lot)
        and lot.current_step is not None
        and setup_change_allowed(model, tool, lot.current_step)
    ]
