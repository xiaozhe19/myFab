"""分支采集用的工具：停点、可存档的存储中继、检查点读写、分支换随机流。

一棵分支树的每个节点都从一个检查点（预热结束或某条分支的中途）出发：
1. ``load_checkpoint`` 读出 (引擎, 中继存储, 控制器)；
2. ``reseed_branch`` 换一套仿真随机流，``retarget_end`` 把仿真结束时刻改成本节点的终点；
3. ``RelayStorage.attach`` 接上本节点自己的 SQLite：先写入改好起止时刻的表头记录
   （tokenizer / 每日指标的 schema），之后的记录直接转发；
4. 跑到终点；途中 ``StopAt`` 在指定时刻中断，``save_checkpoint`` 存档后继续。

SQLite 存储有写库线程，不能存档；``RelayStorage`` 存档时丢掉 target，读档后再接新的。
"""

from __future__ import annotations

from collections import deque
import copy
from dataclasses import replace
from pathlib import Path
import pickle
import random

from fab.plugins.base import FabPlugin, PluginResultContext
from fab.plugins.storage import ResultStorage


class StopAtFork(Exception):
    """时钟越过停点后中断主循环。此时该时刻之后的事件都还没结算。"""

    def __init__(self, time: float) -> None:
        super().__init__(time)
        self.time = time


class StopAt(FabPlugin):
    """时钟第一次越过 ``times`` 中的某个时刻时中断。必须排在所有插件最前面。

    中断前先弹出该时刻：引擎重新进入 advance 时会对同一时刻再回调一次，那时放行。
    """

    def __init__(self, times=()) -> None:
        self.times: list[float] = sorted(float(t) for t in times)

    def set(self, times) -> None:
        self.times = sorted(float(t) for t in times)

    def on_time_advanced(self, current_time, lots) -> None:
        if self.times and current_time > self.times[0]:
            raise StopAtFork(self.times.pop(0))


class RelayStorage(FabPlugin, ResultStorage):
    """转发给当前接上的存储；没接存储时暂存（只在预热期出现）。

    表头记录（tokenizer、每日指标的 schema）另外记一份，每接一个新存储就按该节点
    的起止时刻改写后重新写入，这样每个节点的数据库都是自洽的一段运行。
    """

    HEADERS = (("tokenizer", "schema"), ("daily_metrics", "schema"))

    def __init__(self) -> None:
        self.headers: dict[tuple[str, str], tuple[dict, float | None]] = {}
        self.pending: list[tuple[str, tuple]] = []
        self.target: ResultStorage | None = None
        self.start_time: float | None = None

    def __getstate__(self) -> dict:
        if self.pending:
            raise RuntimeError("flush pending records before saving a checkpoint")
        state = dict(self.__dict__)
        state["target"] = None
        return state

    def provided_services(self) -> dict[str, object]:
        return {"result_storage": self}

    def flush_to(self, target: ResultStorage, strategy_name: str) -> None:
        """把暂存的记录原样写进 ``target`` 并结束它（预热数据库）。"""

        target.start_run()
        for name, args in self.pending:
            getattr(target, name)(*args)
        target.finish_run(strategy_name)
        self.pending = []

    def attach(self, target: ResultStorage, *, start_time: float, end_time: float) -> None:
        if self.pending:
            raise RuntimeError("flush pending warm-up records first")
        target.start_run()
        for (plugin, record_type), (payload, _time) in self.headers.items():
            payload = dict(payload)
            if plugin == "tokenizer":
                payload.update(first_sample_time=start_time, simulation_end_time=end_time)
            else:
                payload.update(start_time=start_time, end_time=end_time)
            target.append_extension_data(plugin, record_type, payload, start_time)
        self.target = target
        self.start_time = start_time

    def _call(self, name: str, *args) -> None:
        if self.target is not None:
            getattr(self.target, name)(*args)
        else:
            self.pending.append((name, args))

    # 引擎回调：start_run 由 attach 负责，finish_run 只转发给已接上的存储。
    def on_simulation_started(self, model) -> None:
        pass

    def on_simulation_finished(self, context: PluginResultContext) -> None:
        self.finish_run(context.strategy_name)

    def start_run(self) -> None:
        pass

    def append_operation(self, record) -> None:
        self._call("append_operation", record)

    def append_lot_event(self, record) -> None:
        self._call("append_lot_event", record)

    def append_metrics(self, measurement) -> None:
        self._call("append_metrics", measurement)

    def append_extension_data(self, plugin_name, record_type, payload, event_time=None) -> None:
        if (plugin_name, record_type) in self.HEADERS:
            self.headers[(plugin_name, record_type)] = (copy.deepcopy(payload), event_time)
        if (record_type == "daily_summary" and self.start_time is not None and event_time is not None
                and event_time <= self.start_time):
            # 停点触发时，截至分叉时刻那一天的汇总可能还没写；它属于父分支，不进本分支的库。
            return
        self._call("append_extension_data", plugin_name, record_type, payload, event_time)

    def finish_run(self, strategy_name: str) -> None:
        if self.target is not None:
            self.target.finish_run(strategy_name)


def save_checkpoint(path: Path, objects) -> int:
    """存档 ``objects``（含引擎）。先写临时文件再改名，返回字节数。"""

    for obj in objects:
        if hasattr(obj, "_completion_snapshot_ref"):
            obj._completion_snapshot_ref = None  # 引擎里的 weakref 缓存，不能存档，可重建
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        pickle.dump(tuple(objects), stream, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(path)
    return path.stat().st_size


def load_checkpoint(path: Path):
    with Path(path).open("rb") as stream:
        return pickle.load(stream)


def reseed_branch(engine, tag: str) -> None:
    """分支换一套随机流（加工、宕机、返工、投料），共享出发状态但运气独立。

    已排进日历的事件（已抽好的时长）不变。
    """

    engine._seed = f"{engine._seed}|{tag}"
    engine._lot_rngs.clear()
    engine._tool_rngs.clear()
    engine.order_source.rng = random.Random(f"{engine._seed}|release")


def _paths_to(target, root, *, skip) -> list[str]:
    """从 root 出发遍历对象图，返回所有指向 target 的属性路径（不进入 skip 和基本类型）。"""

    atoms = (str, bytes, int, float, bool, type(None), complex, range)
    found, seen = [], {id(skip), id(target)}
    stack = [(root, "engine")]
    while stack:
        obj, path = stack.pop()
        if id(obj) in seen or isinstance(obj, atoms) or callable(obj) and not hasattr(obj, "__dict__"):
            continue
        seen.add(id(obj))
        if isinstance(obj, dict):
            children = ((f"{path}[{k!r}]", v) for k, v in obj.items())
        elif isinstance(obj, (list, tuple, set, frozenset, deque)):
            children = ((f"{path}[]", v) for v in obj)
        else:
            children = []
            if hasattr(obj, "__dict__"):
                children += [(f"{path}.{k}", v) for k, v in vars(obj).items()]
            for slot in getattr(type(obj), "__slots__", ()):
                if hasattr(obj, slot):
                    children.append((f"{path}.{slot}", getattr(obj, slot)))
        for child_path, child in children:
            if child is target:
                found.append(child_path)
            else:
                stack.append((child, child_path))
    return found


def retarget_end(engine, end_time: float) -> None:
    """把仿真终点改成 ``end_time``（只能提前）：换掉所有对旧模型对象的引用，补一个结束事件。"""

    from fab.engine.events import EventKind

    old = engine.model
    if not engine.current_time <= end_time <= old.simulation.end_time:
        raise ValueError("new end must lie between now and the current end")
    if end_time == old.simulation.end_time:
        return
    new = replace(old, simulation=replace(old.simulation, end_time=end_time))
    holders = [engine, engine._strategy, *engine._plugin_manager.plugins]
    holders += [v for h in list(holders) for v in vars(h).values() if hasattr(v, "__dict__")]
    for holder in holders:
        for name, value in vars(holder).items():
            if value is old:
                setattr(holder, name, new)
    # 每个持有旧模型的对象都必须换掉，否则各处看到的终点不一致。
    leftovers = _paths_to(old, engine, skip=new)
    if leftovers:
        raise RuntimeError(f"model still referenced at {leftovers[:5]}")
    engine.schedule(end_time, EventKind.SIMULATION_END)
