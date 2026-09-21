"""SMT2020 离散事件日历。

事件按 ``time -> priority -> sequence`` 排序。priority 只处理同一时刻的
因果关系；sequence 让完全相同的事件仍保持可复现顺序。
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class EventKind(str, Enum):
    """由 SMT2020 路线、设备可用性和投料规则触发的状态变化。"""

    # 投料源：ORDER_ARRIVAL 用于旧教学版自动生成；LOT_AVAILABLE 用于外部计划导入。
    ORDER_ARRIVAL = "order_arrival"
    LOT_AVAILABLE = "lot_available"
    LOT_RELEASE = "lot_release"

    # Toolgroups 与 Route_Product_i 的设备占用链。
    LOAD_COMPLETE = "load_complete"
    SETUP_COMPLETE = "setup_complete"
    OPERATION_COMPLETE = "operation_complete"
    UNLOAD_COMPLETE = "unload_complete"
    TRANSPORT_COMPLETE = "transport_complete"
    TOOL_AVAILABLE = "tool_available"

    # PM 与 Breakdown。
    PM_DUE = "pm_due"
    PM_COMPLETE = "pm_complete"
    BREAKDOWN = "breakdown"
    REPAIR_COMPLETE = "repair_complete"

    # Route_Product_i 的可选规则。
    BATCH_READY = "batch_ready"
    REWORK_ROUTED = "rework_routed"
    SAMPLING_DECISION = "sampling_decision"
    CQT_EXPIRE = "cqt_expire"

    # 引擎控制事件。
    STRATEGY_WAKEUP = "strategy_wakeup"
    SIMULATION_END = "simulation_end"


EVENT_PRIORITY = {
    # 停机必须先于同一时刻的加工完成结算，才能正确中断设备活动。
    EventKind.BREAKDOWN: 5,
    EventKind.PM_DUE: 5,
    EventKind.LOAD_COMPLETE: 10,
    EventKind.SETUP_COMPLETE: 10,
    EventKind.OPERATION_COMPLETE: 10,
    EventKind.UNLOAD_COMPLETE: 10,
    EventKind.TRANSPORT_COMPLETE: 10,
    EventKind.TOOL_AVAILABLE: 12,
    EventKind.PM_COMPLETE: 15,
    EventKind.REPAIR_COMPLETE: 15,
    EventKind.REWORK_ROUTED: 20,
    EventKind.SAMPLING_DECISION: 20,
    EventKind.BATCH_READY: 25,
    EventKind.CQT_EXPIRE: 25,
    EventKind.ORDER_ARRIVAL: 30,
    EventKind.LOT_AVAILABLE: 30,
    EventKind.LOT_RELEASE: 30,
    EventKind.STRATEGY_WAKEUP: 40,
    EventKind.SIMULATION_END: 100,
}


class SimulationEvent(NamedTuple):
    """事件日历中的只读记录；使用 tuple 的 C 层字典序比较。"""

    time: float
    priority: int
    sequence: int
    kind: EventKind
    # 引擎调度的事件始终显式传入独立 payload；默认值供简单事件和测试使用。
    payload: dict[str, object] = {}
