"""离散分布的训练目标（交叉熵）和评估指标。

loss = λ_C·mean(三列 C 的 CE) + λ_W·CE(ΔW 区间) + λ_Q·CE(ΔQ 区间) + λ_P·组内比例的每 lot NLL

单位都是 nats（自然对数）。组内比例的 NLL = −Σ_格子 下一帧 lot 数 × log 预测比例，
再除以下一帧总 lot 数，即"每个 lot 落在它真实格子里的平均 −log 概率"。
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn

PARTS = ("C", "W", "Q", "P")


def cross_entropy(logits, target):
    return F.cross_entropy(logits.reshape(-1, logits.shape[-1]), target.reshape(-1))


def composition_nll(logits, cells1, *, total=False):
    nll = -(cells1 * logits.log_softmax(-1)).sum()
    lots = cells1.sum()
    return (nll, lots) if total else nll / lots.clamp(min=1)


class FabotronLoss(nn.Module):
    def __init__(self, weights=None):
        super().__init__()
        self.weights = {name: 1.0 for name in PARTS}
        if weights:
            if set(weights) - set(PARTS):
                raise ValueError("Unknown loss weights")
            self.weights.update(weights)
        if any(v < 0 for v in self.weights.values()) or not sum(self.weights.values()):
            raise ValueError("Loss weights must be nonnegative and not all zero")

    def components(self, outputs, targets):
        # C：所有 (组, 列) 的 CE 平均；每个档单独算，再按个数合并
        c_sum = sum(F.cross_entropy(logits.reshape(-1, logits.shape[-1]), classes.reshape(-1), reduction="sum")
                    for logits, classes in zip(outputs["c"], outputs["c_class"]))
        c_count = sum(classes.numel() for classes in outputs["c_class"])
        return {"C": c_sum / max(1, c_count), "W": cross_entropy(outputs["w"], targets["w_bin"]),
                "Q": cross_entropy(outputs["q"], targets["q_bin"]),
                "P": composition_nll(outputs["comp"], targets["cells1"])}

    def combine(self, parts):
        return sum(self.weights[name] * parts[name] for name in PARTS)

    def forward(self, outputs, targets):
        parts = self.components(outputs, targets)
        return self.combine(parts), parts

    def configuration(self):
        return {"weights": dict(self.weights), "units": "nats",
                "C": ("mean CE over (group, column); class = integer change within the group's tier "
                      "(tier >= 1.25x the largest training change + 1); infeasible masked; larger changes clamp to the edge"),
                "W/Q": "CE over 21 sigma-scaled bins of the 6h change",
                "P": "per-lot NLL of the next-frame cell composition within each group"}


def median_value(coder, which, probs):
    """离散分布的中位数区间 -> 代表值 Δ（lot）。MAE 的最优点估计是中位数。"""
    bins = (probs.cumsum(-1) < 0.5).sum(-1).clamp(max=probs.shape[-1] - 1)
    return coder.values(which, bins)


def mean_value(coder, which, probs):
    return (probs * coder.bin_values[which]).sum(-1)


COVERAGE_LEVELS = (0.5, 0.8, 0.9)
PIT_BINS = 10


def crps_discrete(probs, atoms, y):
    """离散分布（原子 atoms、概率 probs，[..., K]）对真实值 y [...] 的 CRPS。

    CRPS = E|X − y| − ½·E|X − X'|。点分布时就是 |x − y|，所以和 MAE 同单位、可直接比。
    """
    first = (probs * (atoms - y.unsqueeze(-1)).abs()).sum(-1)
    pair = (atoms.unsqueeze(-1) - atoms.unsqueeze(-2)).abs()                    # [..., K, K]
    second = torch.einsum("...j,...jk,...k->...", probs, pair, probs)
    return first - 0.5 * second


def pit_overlap(probs, true_bin, low, high):
    """随机化 PIT 落在 [low, high] 的期望概率（对 U 取期望，无随机数）。

    类别分布的 PIT = F(真实区间之前) + U·p(真实区间)，U~均匀(0,1)，即在 [F−, F] 上均匀。
    校准良好时 PIT ~ 均匀(0,1)，所以落进中间 c 的比例应 = c。
    """
    cdf = probs.cumsum(-1)
    upper = cdf.gather(-1, true_bin.unsqueeze(-1)).squeeze(-1)
    p = probs.gather(-1, true_bin.unsqueeze(-1)).squeeze(-1)
    lower = upper - p
    overlap = (torch.minimum(upper, torch.as_tensor(high)) - torch.maximum(lower, torch.as_tensor(low))).clamp(min=0)
    point = ((lower >= low) & (lower < high)).float()
    return torch.where(p > 1e-9, overlap / p.clamp(min=1e-9), point)


def central_width(probs, atoms, level):
    """中间 level 区间 [q_(1−level)/2, q_(1+level)/2] 的宽度（lot），衡量分布有多"尖"。"""
    cdf = probs.cumsum(-1)
    last = probs.shape[-1] - 1
    lo = (cdf < (1 - level) / 2).sum(-1, keepdim=True).clamp(max=last)
    hi = (cdf < (1 + level) / 2).sum(-1, keepdim=True).clamp(max=last)
    return (atoms.gather(-1, hi) - atoms.gather(-1, lo)).squeeze(-1)


class StateMetrics:
    """累计点估计误差（可与旧基线比）+ 联合 NLL（只有模型有）。

    update(targets, probs, outputs)：
    - probs：``Fabotron.predict`` 的单步边际分布，用来算点估计；None = 持续性基线；
    - outputs：teacher-forcing 的 logits，用来算联合 NLL = −log p(C) − log p(W|C) − log p(Q|C,W) − 比例 NLL。
    """

    def __init__(self, coder, objective=None):
        self.coder, self.objective = coder, objective
        self.count = 0
        self.sums = {}
        self.parts = {name: 0.0 for name in PARTS}
        self.tv_sum, self.tv_count = 0.0, 0
        self.pit = {}

    def _distribution(self, name, probs, atoms, true_bin, y, size):
        """CRPS、覆盖率、区间宽度、PIT 直方图；按 (样本, 组) 平均。"""
        self._add(f"{name}_crps_lots", crps_discrete(probs, atoms, y).mean(), size)
        for level in COVERAGE_LEVELS:
            tag = int(round(level * 100))
            low, high = (1 - level) / 2, (1 + level) / 2
            self._add(f"{name}_coverage_{tag}", pit_overlap(probs, true_bin, low, high).mean(), size)
            self._add(f"{name}_width_{tag}_lots", central_width(probs, atoms, level).float().mean(), size)
        hist = torch.stack([pit_overlap(probs, true_bin, j / PIT_BINS, (j + 1) / PIT_BINS).mean()
                            for j in range(PIT_BINS)]) * size
        self.pit[name] = self.pit.get(name, 0) + hist

    def _add(self, name, value, size):
        self.sums[name] = self.sums.get(name, 0.0) + value.detach().double() * size

    @torch.no_grad()
    def update(self, targets, probs=None, outputs=None):
        coder, layout = self.coder, self.coder.layout
        size = len(targets["w0"])
        w0, q0, w1, q1 = targets["w0"], targets["q0"], targets["w1"], targets["q1"]
        capacity = layout.capacity
        if outputs is not None:
            for name, value in self.objective.components(outputs, targets).items():
                self.parts[name] += value.detach().double() * size
        if probs is None:
            w_hat, q_hat, w_mean, q_mean = w0, q0, w0, q0
            c_hat = targets["c0"].float()
            cells0 = targets["cells0"]
            comp = torch.where(cells0.sum(-1, keepdim=True) > 0, cells0 / cells0.sum(-1, keepdim=True).clamp(min=1),
                               layout.cell_mask / layout.cell_mask.sum(-1, keepdim=True))
        else:
            w_hat, q_hat = w0 + median_value(coder, 0, probs["w"]), q0 + median_value(coder, 1, probs["q"])
            w_mean, q_mean = w0 + mean_value(coder, 0, probs["w"]), q0 + mean_value(coder, 1, probs["q"])
            c_hat = probs["c_hat"]
            comp = probs["comp"]
        # 分布指标：原子 = 下一帧可能的取值（与 Fabotron.sample 一致：代表值取整、不小于 0）
        for which, name, x0, x1 in ((0, "wip", w0, w1), (1, "queue", q0, q1)):
            true_bin = targets["w_bin" if which == 0 else "q_bin"]
            if probs is None:   # 持续性 = 全部概率压在 x0 上（CRPS 退化为 MAE）
                bins = self.coder.bins
                dist = F.one_hot(coder.bucket(torch.zeros_like(x0), which), bins).float()
                atoms = x0.unsqueeze(-1).expand(*x0.shape, bins)
            else:
                dist = probs["w" if which == 0 else "q"]
                atoms = (x0.unsqueeze(-1) + coder.bin_values[which].round()).clamp(min=0)
            self._distribution(name, dist, atoms.float(), true_bin, x1.float(), size)
        self._add("wip_mae_lots", (w_hat - w1).abs().mean(), size)
        self._add("queue_mae_lots", (q_hat - q1).abs().mean(), size)
        self._add("wip_mae_lots_mean_estimate", (w_mean - w1).abs().mean(), size)
        self._add("queue_mae_lots_mean_estimate", (q_mean - q1).abs().mean(), size)
        # 与旧 E 指标同口径：三列比例的 MSE
        self._add("equipment_mse", ((c_hat - targets["c1"]) / capacity).square().mean(), size)
        self._add("equipment_mae_count", (c_hat - targets["c1"]).abs().mean(), size)
        # 组内比例 TV（与旧 wip_composition_tv_projected 同一组类别），只算下一帧 W > 0 的组
        cells1 = targets["cells1"]
        mass = cells1.sum(-1)
        valid = mass > 0
        actual = cells1 / mass.clamp(min=1).unsqueeze(-1)
        tv = 0.5 * (comp - actual).abs().sum(-1)
        self.tv_sum += (tv * valid).sum().double()
        self.tv_count += valid.sum()
        self.count += size

    def result(self):
        if not self.count:
            raise ValueError("Empty evaluation set")
        tv_count = int(self.tv_count)
        result = {"samples": self.count, **{name: float(value / self.count) for name, value in self.sums.items()},
                  "composition_tv": float(self.tv_sum / tv_count) if tv_count else None,
                  **{f"{name}_pit_hist": (hist / self.count).tolist() for name, hist in self.pit.items()}}
        if self.objective is not None:
            parts = {name: float(value / self.count) for name, value in self.parts.items()}
            result["components"] = parts
            result["loss"] = float(self.objective.combine(parts))
        return result
