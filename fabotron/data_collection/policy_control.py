"""采集时的热门组选择与权重生成（Fabotron 阶段 2）。

每 6 小时一次控制更新：
1. 瓶颈分数（Cao et al. 2011）：QLC = 排队 lot 的设备占用时长之和 / 设备数（小时），
   IBD = 1 - exp(-λ·QLC)，BD 按 α 指数平滑，score = sqrt(b_m · BD)。b_m 为静态
   相对负荷（含换型占比），离线算好放在 strategy/config/hot_groups_smt2020.json。
2. 热门组 = 核心组（固定）+ 浮动名额。浮动组按非核心组内的分数排名进入；
   排名跌出 exit_rank 且已停留至少 min_dwell_updates 次更新才退出。
3. 每个热门组持有一组 6 维权重（strategy.mixed.RULES），保持 1–3 天后重抽；
   新进入的组立即抽一组。没选中的组用 FIFO_SA。

token 的策略槽为 VOCAB = ("FIFO_SA", *RULES)：没选中的组 one-hot 在 FIFO_SA，
热门组为 (0, w...)。权重生成用独立的随机流，不消耗仿真的 CRN 流。
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from fab.model.entities import FabModel, RouteStepSpec
from fab.plugins.base import FabPlugin
from strategy.mixed import RULES, MixedRule

VOCAB = ("FIFO_SA", *RULES)
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "strategy" / "config" / "hot_groups_smt2020.json"
_FIFO_SA = tuple(float(i == 0) for i in range(len(VOCAB)))


def occupancy(step: RouteStepSpec, wafers: int) -> float:
    """一个 lot 在该工步占用设备的分钟数（与离线 cao_score.py 相同的口径）。"""

    if step.processing_unit == "batch":
        return step.process_time * wafers / step.batch_maximum if step.batch_maximum else step.process_time
    # 级联设备：下一个 lot 在本 lot 开始后 (n-1)·interval（按片）或 interval（按 lot）即可进入。
    if step.processing_unit == "wafer":
        if step.cascading_interval:
            return (wafers - 1) * step.cascading_interval
        return wafers * step.process_time
    return step.cascading_interval if step.cascading_interval else step.process_time


class HotGroupSelector:
    """核心组 + 浮动名额 + 滞回 + 最短停留。只含纯逻辑，便于测试。"""

    def __init__(self, config: dict, groups: list[str]) -> None:
        self.groups = [g for g in groups if g in config["b_m"]]
        self.b_m = {g: float(config["b_m"][g]) for g in self.groups}
        self.lam = float(config["lambda_per_hour"])
        self.alpha = float(config["alpha"])
        self.core = [g for g in config["core"] if g in self.b_m]
        self.floating = int(config["n_selected"]) - len(self.core)
        self.exit_rank = int(config["exit_rank"])
        self.dwell = int(config["min_dwell_updates"])
        self.bd: dict[str, float] | None = None
        self.members: dict[str, int] = {}  # 浮动组 -> 已停留的更新次数

    def update_bd(self, qlc_hours: dict[str, float]) -> None:
        ibd = {g: 1.0 - math.exp(-self.lam * qlc_hours.get(g, 0.0)) for g in self.groups}
        if self.bd is None:
            self.bd = ibd
        else:
            a = self.alpha
            self.bd = {g: a * ibd[g] + (1 - a) * self.bd[g] for g in self.groups}

    def scores(self) -> dict[str, float]:
        assert self.bd is not None
        return {g: math.sqrt(self.b_m[g] * self.bd[g]) for g in self.groups}

    def select(self) -> list[str]:
        """按当前 BD 更新浮动成员，返回全部热门组（核心组在前）。"""

        score = self.scores()
        core = set(self.core)
        # 分数相同时按组名，保证结果确定。
        order = sorted((g for g in self.groups if g not in core), key=lambda g: (-score[g], g))
        rank = {g: r for r, g in enumerate(order, 1)}
        for g in list(self.members):
            self.members[g] += 1
            if rank[g] > self.exit_rank and self.members[g] >= self.dwell:
                del self.members[g]
        for g in order[: self.floating]:
            if len(self.members) >= self.floating:
                break
            self.members.setdefault(g, 0)
        return [*self.core, *sorted(self.members, key=lambda g: rank[g])]


class WeightSampler:
    """为热门组抽 6 维权重，三类各占一定比例：纯规则、随机混合、接近 FIFO_SA。"""

    def __init__(
        self,
        rng: random.Random,
        mix: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3),
        hold_updates: tuple[int, int] = (4, 12),
        near_fifo_sa_noise: float = 0.2,
    ) -> None:
        self.rng = rng
        self.mix = mix
        self.hold_updates = hold_updates
        self.noise = near_fifo_sa_noise

    def _dirichlet(self) -> list[float]:
        values = [self.rng.gammavariate(1.0, 1.0) for _ in RULES]
        total = sum(values)
        return [v / total for v in values]

    def draw(self) -> tuple[tuple[float, ...], int]:
        """返回 (权重, 保持的更新次数)。"""

        kind = self.rng.choices(range(3), weights=self.mix)[0]
        if kind == 0:
            k = self.rng.randrange(len(RULES))
            weights = [float(i == k) for i in range(len(RULES))]
        elif kind == 1:
            weights = self._dirichlet()
        else:
            # FIFO 与 SETUP 各半（打分版的 FIFO_SA），再混入少量随机权重。
            base = [0.5 if rule in ("FIFO", "SETUP") else 0.0 for rule in RULES]
            noise = self._dirichlet()
            weights = [(1 - self.noise) * b + self.noise * n for b, n in zip(base, noise)]
        hold = self.rng.randint(*self.hold_updates)
        return tuple(weights), hold


class PolicyController(FabPlugin):
    """每 6 小时更新 BD；激活后选热门组、维护权重，并写入 MixedRule。

    插件顺序必须在 TokenizerPlugin 之前：同一次时钟回调里先更新本窗口的
    权重，Tokenizer 随后记录的 token 才带着"下一窗口要执行的策略"。
    """

    def __init__(
        self,
        model: FabModel,
        strategy: MixedRule,
        *,
        bd_start_time: float,
        interval_minutes: float = 360.0,
        config_path: Path = DEFAULT_CONFIG,
    ) -> None:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.model = model
        self.strategy = strategy
        self.interval = float(interval_minutes)
        self.next_time = float(bd_start_time)
        self.selector = HotGroupSelector(config, sorted(model.tool_groups))
        self.tools = {g: max(1, spec.number_of_tools) for g, spec in model.tool_groups.items()}
        self.engine = None
        self.sampler: WeightSampler | None = None
        self.weights: dict[str, tuple[float, ...]] = {}
        self.remaining: dict[str, int] = {}
        self.log: list[dict[str, object]] = []
        self._occupancy_cache: dict[tuple[int, int], float] = {}

    def bind_engine(self, engine) -> None:
        self.engine = engine

    def activate(self, rng_seed: str, **sampler_options) -> None:
        """从下一次控制更新起开始选组、抽权重。分支复制后调用。"""

        self.sampler = WeightSampler(random.Random(rng_seed), **sampler_options)

    # ------------------------------------------------------------ 时钟回调

    def on_time_advanced(self, current_time: float, lots) -> None:
        # 与 Tokenizer 相同：时钟第一次越过控制时刻时，状态就是该时刻的状态。
        while current_time > self.next_time:
            self._update(self.next_time)
            self.next_time += self.interval

    def _qlc_hours(self) -> dict[str, float]:
        engine = self.engine
        work: dict[str, float] = {}
        cache = self._occupancy_cache
        for lot_id, group in engine._ready_group_by_lot.items():
            lot = engine._lots_by_id[lot_id]
            step = lot.route[lot.operation_index]
            key = (id(step), lot.wafer_count)
            minutes = cache.get(key)
            if minutes is None:
                minutes = cache[key] = occupancy(step, lot.wafer_count)
            work[group] = work.get(group, 0.0) + minutes
        return {g: w / self.tools.get(g, 1) / 60.0 for g, w in work.items()}

    def _update(self, time: float) -> None:
        self.selector.update_bd(self._qlc_hours())
        if self.sampler is None:
            return
        hot = self.selector.select()
        for g in list(self.weights):
            if g not in hot:
                del self.weights[g], self.remaining[g]
        for g in hot:
            left = self.remaining.get(g, 0) - 1
            if g not in self.weights or left <= 0:
                self.weights[g], self.remaining[g] = self.sampler.draw()
            else:
                self.remaining[g] = left
        self.strategy.set_schedule(
            [{"start_day": 0.0, "weights": {g: dict(zip(RULES, w)) for g, w in self.weights.items()}}]
        )
        self.log.append({"time": time, "hot": hot})

    # ------------------------------------------------------- Tokenizer 接口

    def policy(self, view) -> dict[str, tuple[float, ...]]:
        """每组的策略向量（VOCAB 顺序），给 TokenizerPlugin 的 dispatch_policy。"""

        return {
            g: ((0.0, *self.weights[g]) if g in self.weights else _FIFO_SA)
            for g in self.model.tool_groups
        }
