"""派工前的资格检查，不包含任何调度优先级或选择规则。"""

from __future__ import annotations

from fab.model.entities import FabModel, LotState, RouteStepSpec, ToolState


def dedication_allowed(lot: LotState, tool_id: str) -> bool:
    """lot-to-lens dedication：仅当当前工步是某个进行中 LTL 绑定的目标时，
    强制回绑定设备；否则自由派到同工具组任意设备。

    支持链式绑定：``lot.dedicated_tools`` 是 {目标工步 id: 绑定设备 id}，
    当前工步 id 命中该映射时才受约束。
    """

    if not lot.dedicated_tools:
        return True
    step = lot.current_step
    if step is None:
        return True
    bound_tool = lot.dedicated_tools.get(step.id)
    if bound_tool is None:
        return True
    return bound_tool == tool_id


def tool_can_process(model: FabModel, tool_id: str, lot: LotState) -> bool:
    """检查静态工具组资格及 lot-to-lens dedication。

    ``tool_group_id`` 是唯一的设备加工资格约束；``process`` 只是供 metrics
    和策略进行工艺/区域统计的分类字段，不能再作为第二套资格条件。
    """

    step = lot.current_step
    tool = model.tools.get(tool_id)
    if step is None or tool is None:
        return False
    if step.tool_group_id is None or tool.tool_group_id is None:
        return False
    if step.tool_group_id != tool.tool_group_id:
        return False
    return dedication_allowed(lot, tool_id)


def tool_is_available(tool: ToolState, current_time: float) -> bool:
    """检查设备是否未加工、未故障、未维护/换型且已经可用。"""

    return (
        not tool.in_process
        and not tool.is_down
        and not tool.is_in_setup
        and tool.available_time <= current_time
    )


def setup_change_allowed(model: FabModel, tool: ToolState, step: RouteStepSpec) -> bool:
    """执行 setup minimum run length 这一硬约束。

    SMT 语义：min_run 是"切换后必须连续加工 X 个 lot 才能再切走"，
    是切换后的保留约束，不是切换的前置条件。因此设备从空 setup（初始
    None/""）首次切到某 setup 总是允许；只有已处于某 setup 再切换时，
    才要求当前 setup 已连续加工的 lot 数 >= min_run。
    """

    required_setup = step.required_setup
    if required_setup is None or required_setup == tool.current_setup:
        return True
    # 设备尚未 setup（初始 None/""）：首次切换无前置约束。
    if not tool.current_setup:
        return True
    minimum_run = max(
        (
            transition.minimal_run_length
            for transition in model.setup_transitions
            if transition.current_setup == tool.current_setup
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
