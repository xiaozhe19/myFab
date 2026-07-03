from __future__ import annotations

from collections import Counter
from typing import Any

from fab.core import (
    FabStrategyBase,
    FactoryState,
    MachineState,
    WaferState,
    ready_candidates,
)

# 第一版学习用 DBR：先固定 Drum 为 Litho。
# 后续如果要做动态 Drum，建议新建 DBR_v2.py 或新增 drum 识别模块。
DRUM_PROCESS = "Litho"

# Litho 前保护量的三个阈值，单位和配置文件一致：minute。
RED_BUFFER_TIME = 100
TARGET_BUFFER_TIME = 300
OVER_BUFFER_TIME = 500

RELEASE_INTERVAL = 38.0


def calculate_drum_buffer_time(
    wafers: list[WaferState],
    current_time: float,
    drum_process: str = DRUM_PROCESS,
) -> float:
    """
    计算 Drum 前面的 buffer time。

    第一版定义：
    当前已经 ready、且当前工序就是 Drum 的 wafer，它们的 Drum 加工时间总和。

    例子：
    Litho 前有三片 ready wafer，Litho 加工时间分别是 18、20、22 分钟，
    那么 buffer time = 60 分钟。
    """

    buffer_time = 0.0
    for wafer in wafers:
        if wafer.completed:
            continue
        if wafer.ready_time > current_time:
            continue
        if wafer.current_process != drum_process:
            continue
        buffer_time += float(wafer.current_process_time or 0.0)
    return round(buffer_time, 3)


def calculate_drum_buffer_count(
    wafers: list[WaferState],
    current_time: float,
    drum_process: str = DRUM_PROCESS,
) -> int:
    """计算 Drum 前当前已经 ready 的 wafer 数量。"""

    return sum(
        1
        for wafer in wafers
        if not wafer.completed
        and wafer.ready_time <= current_time
        and wafer.current_process == drum_process
    )


def classify_buffer(buffer_time: float) -> str:
    """
    把 Drum buffer time 分成几个状态。

    这个函数先把结构搭出来，后续你可以自己调整阈值。
    """

    if buffer_time < RED_BUFFER_TIME:
        return "red"
    if buffer_time < TARGET_BUFFER_TIME:
        return "yellow"
    if buffer_time <= OVER_BUFFER_TIME:
        return "green"
    return "over"


def steps_until_drum(wafer: WaferState, drum_process: str = DRUM_PROCESS) -> int | None:
    """
    判断 wafer 从当前 step 开始，还要几步才会到 Drum。

    返回值解释：
    - 0：当前就在 Drum 前面
    - 1：做完当前这一步，下一步就是 Drum
    - None：后续路线不再经过 Drum

    这是 DBR dispatch 会用到的重要辅助函数。
    """

    if wafer.completed:
        return None
    for offset, operation in enumerate(wafer.route[wafer.step :]):
        if operation["process"] == drum_process:
            return offset
    return None


def work_until_drum(
    wafer: WaferState,
    drum_process: str = DRUM_PROCESS,
) -> float | None:
    """
    估计 wafer 从当前 step 到下一次 Drum 前还需要多少非 Drum 加工时间。

    返回 0 表示当前已经在 Drum 前等待。
    返回 None 表示后续路线里不再经过 Drum。
    """

    if wafer.completed:
        return None

    work = 0.0
    for operation in wafer.route[wafer.step :]:
        if operation["process"] == drum_process:
            return round(work, 3)
        work += float(operation["process_time"])
    return None


def dbr_sample_hook(
    current_time: float,
    wafers: list[WaferState],
) -> dict[str, Any]:
    """
    DBR 专属采样函数。

    通用引擎会在每个采样时刻调用它，并把返回内容追加进结果 JSON。
    这里先记录 Litho buffer 的时间和状态。
    """

    buffer_time = calculate_drum_buffer_time(wafers, current_time)
    return {
        "buffer_samples": {
            "time": current_time,
            "drum": DRUM_PROCESS,
            "buffer_time": buffer_time,
            "buffer_count": calculate_drum_buffer_count(wafers, current_time),
            "state": classify_buffer(buffer_time),
        }
    }


def select_dbr_job(
    wafers: list[WaferState],
    machine: MachineState,
    current_time: float,
    reserved_wafer_ids: set[str] | None = None,
) -> WaferState | None:
    """
    DBR v1 的派工函数。

    现在整个文件里最重要的就是这个函数。
    通用仿真引擎已经帮你处理了机器、setup、downtime、时间推进和结果保存；
    这里你只需要决定“当前这台机器从候选 wafer 中选谁”。
    """

    reserved_wafer_ids = reserved_wafer_ids or set()
    candidates = [
        wafer
        for wafer in ready_candidates(wafers, machine, current_time)
        if wafer.id not in reserved_wafer_ids
    ]
    if not candidates:
        return None

    buffer_time = calculate_drum_buffer_time(wafers, current_time)
    buffer_state = classify_buffer(buffer_time)
    product_counts = Counter(wafer.product_id for wafer in candidates)

    def batching_key(wafer: WaferState) -> tuple[Any, ...]:
        """
        在 DBR 大方向相同的候选里做 batching。

        先尽量延续机器当前产品，减少换型；如果当前产品没有候选，
        再偏向当前队列里数量更多的产品族。
        """

        same_product_rank = 0 if wafer.product_id == machine.current_product_id else 1
        return (same_product_rank, -product_counts[wafer.product_id])

    # min() 会选择排序 key 最小的 wafer，所以“越优先”的条件给越小的数字。
    def dbr_sort_key(wafer: WaferState) -> tuple[Any, ...]:
        distance_to_drum = steps_until_drum(wafer)
        time_to_drum = work_until_drum(wafer)
        has_future_drum = time_to_drum is not None

        # Drum 机器自身先保持 FIFO，让瓶颈持续吃货。
        if machine.type == DRUM_PROCESS:
            return (
                *batching_key(wafer),
                wafer.ready_time,
                wafer.release_time,
                wafer.input_order,
                wafer.id,
            )

        # Drum buffer 偏低时，非 Drum 工序优先补 Litho。
        if buffer_state == "red":
            drum_support_rank = 0 if has_future_drum else 1
            drum_distance = distance_to_drum if distance_to_drum is not None else 999
            drum_work = time_to_drum if time_to_drum is not None else 999999.0
            return (
                drum_support_rank,
                drum_work,
                drum_distance,
                *batching_key(wafer),
                wafer.ready_time,
                wafer.release_time,
                wafer.input_order,
                wafer.id,
            )

        # Drum buffer 过高时，非 Drum 工序暂缓继续压 Litho。
        if buffer_state == "over":
            drum_pressure_rank = 1 if has_future_drum else 0
            drum_work = time_to_drum if time_to_drum is not None else 999999.0
            return (
                drum_pressure_rank,
                -drum_work,
                *batching_key(wafer),
                wafer.ready_time,
                wafer.release_time,
                wafer.input_order,
                wafer.id,
            )

        # yellow / green：保持普通 FIFO。
        return (
            *batching_key(wafer),
            wafer.ready_time,
            wafer.release_time,
            wafer.input_order,
            wafer.id,
        )

    return min(candidates, key=dbr_sort_key)


class DBRV1Strategy(FabStrategyBase):
    """DBR v1 策略对象，统一通过 push_next 向 Engine 提交决策。"""

    name = "DBR v1"
    run_id = "dbr-v1"
    release_interval = RELEASE_INTERVAL

    def select_job(
        self,
        state: FactoryState,
        machine: MachineState,
        reserved_wafer_ids: set[str],
    ) -> WaferState | None:
        return select_dbr_job(
            state.wafers,
            machine,
            state.current_time,
            reserved_wafer_ids,
        )

    def sample(self, state: FactoryState) -> dict[str, Any]:
        return dbr_sample_hook(state.current_time, state.wafers)

    def result_fields(self) -> dict[str, Any]:
        return {
            "drum_process": DRUM_PROCESS,
            "release_policy": {
                "interval": self.release_interval,
                "description": "DBR v1 fixed release interval.",
            },
            "buffer_policy": {
                "red_buffer_time": RED_BUFFER_TIME,
                "target_buffer_time": TARGET_BUFFER_TIME,
                "over_buffer_time": OVER_BUFFER_TIME,
            },
        }
