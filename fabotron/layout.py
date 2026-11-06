"""Token V2 的静态结构 + 预测目标的编码。

- ``TokenLayout``：由 schema 推出的不变量（工步 -> 组、组内格子排布、设备台数）。
  一个"格子"= 一道路线工步 × 一个阶段。每组的格子数不同，这里按组补齐成
  [组数, 最大格子数] 的矩阵，补出来的位置用 mask 标掉。
- ``TargetCoder``：把相邻两帧变成模型要预测的离散类别，并保存训练集统计
  （每组 σ、每个区间的代表值、先验频率）。它是模型的一部分，随 checkpoint 保存。
"""

from __future__ import annotations

import torch
from torch import nn

V2 = "tool-group-token-v2"
# ΔW/ΔQ 按 z = Δ / σ_g 切区间：0 附近细、两头粗；两端各一个"超出"区间，共 21 个。
Z_EDGES = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)
# C 块按台数互斥：空闲 + 忙 + 停机 = 总台数（total_tools 永远是 1）。
# 只预测空闲和停机两列，忙 = 总 − 空闲 − 停机，由它们算出。
C_TARGETS = ("idle_tools", "down_tools")
C_BUSY = "busy_tools"
# C 的分档：每组每列的"范围" r = 训练集里 6h 最大 |Δ| × 1.25 + 1（不超过容量），
# 归到不小于 r 的最小一档 R；同一档共用一个 2R+1 类的头，类别 j 表示 Δ = j − R。
C_TIERS = (2, 4, 8, 16, 32, 64, 128, 256, 512)
C_HIST = C_TIERS[-1]
# 组内格子数的分档上界（最后一档到最大格子数）；SMT2020 下补位后真实格子占比 11% -> 54%。
CELL_BUCKETS = (16, 64, 256)


class TokenLayout(nn.Module):
    """只有 buffer、没有参数；随模型一起 .to(device)。"""

    def __init__(self, schema: dict) -> None:
        super().__init__()
        if schema.get("version") != V2:
            raise ValueError(f"expected {V2} schema")
        groups = {g: i for i, g in enumerate(schema["group_ids"])}
        steps, stages = schema["step_counts"]["shape"]
        if steps != len(schema["steps"]) or stages != len(schema["stages"]):
            raise ValueError("step table shape does not match steps/stages")
        self.groups, self.steps, self.stages = len(groups), steps, stages
        self.ready = schema["stages"].index("Ready")

        step_group = [groups[group] for _p, _i, group, _r, _v in schema["steps"]]
        cells = [[] for _ in groups]                   # 每组拥有的格子（展平下标 = 工步 * 阶段数 + 阶段）
        for step, g in enumerate(step_group):
            cells[g].extend(step * stages + k for k in range(stages))
        self.max_cells = max(len(c) for c in cells)
        pad = steps * stages                            # 指向额外补的一列 0
        index = torch.full((self.groups, self.max_cells), pad, dtype=torch.long)
        for g, owned in enumerate(cells):
            index[g, :len(owned)] = torch.tensor(owned)
        self.register_buffer("step_group", torch.tensor(step_group), persistent=False)
        self.register_buffer("cell_index", index, persistent=False)
        self.register_buffer("cell_mask", index != pad, persistent=False)

        # 格子分档：各组格子数差别很大（7 ~ 546，中位 28），全按 546 补齐时 89% 是补位。
        # 按格子数把组分进几档，每档只补到本档最大格子数；带 d 维的计算都按档做。
        sizes = [len(c) for c in cells]
        self.cell_buckets = []                          # [(本档组数, 本档宽度)]
        order, lower = [], 0
        for upper in [*CELL_BUCKETS, self.max_cells]:
            members = [g for g, n in enumerate(sizes) if lower < n <= upper]
            lower = upper
            if not members:
                continue
            k = len(self.cell_buckets)
            width = max(sizes[g] for g in members)
            self.register_buffer(f"bucket_groups_{k}", torch.tensor(members), persistent=False)
            self.register_buffer(f"bucket_index_{k}", index[members, :width].clone(), persistent=False)
            self.cell_buckets.append((len(members), width))
            order += members
        inverse = torch.empty(len(order), dtype=torch.long)
        inverse[torch.tensor(order)] = torch.arange(len(order))
        self.register_buffer("bucket_inverse", inverse, persistent=False)

        # 设备：C 是比例，乘回分母（总台数）就是整数台数。
        slices = schema["group_features"]["slices"]
        self.c_start = slices["C"][0]
        columns = list(schema["C_columns"])
        if C_BUSY not in columns or any(name not in columns for name in C_TARGETS):
            raise ValueError("schema C block must be (total, idle, busy, down) tool counts; convert old data")
        self.c_columns = [self.c_start + columns.index(name) for name in C_TARGETS]
        denominators = {row[0]: row[1:] for row in schema["normalization"]["C"]}
        capacity = [[denominators[g][columns.index(name)] for name in C_TARGETS] for g in schema["group_ids"]]
        self.register_buffer("capacity", torch.tensor(capacity, dtype=torch.float32), persistent=False)
        self.max_capacity = [int(c) for c in self.capacity.max(0).values.tolist()]
        self.total_tools_column = self.c_start + columns.index("total_tools")
        self.features = schema["group_features"]["shape"][1]
        self.wip_scale = float(schema["normalization"]["W"])
        self.queue_scale = float(schema["normalization"]["Q"])

    def totals(self, counts: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        """工步表 [..., S, K] -> 每组 W、Q [..., G]（按"工步 -> 组"加起来）。"""
        lead = counts.shape[:-2]
        flat = counts.reshape(-1, self.steps, self.stages).float()
        zeros = flat.new_zeros(flat.shape[0], self.groups)
        wip = zeros.index_add(1, self.step_group, flat.sum(-1))
        queue = zeros.index_add(1, self.step_group, flat[..., self.ready])
        return wip.reshape(*lead, self.groups), queue.reshape(*lead, self.groups)

    def cells(self, counts: torch.Tensor) -> torch.Tensor:
        """工步表 [..., S, K] -> 按组排好的格子 [..., G, 最大格子数]，补位为 0。"""
        lead = counts.shape[:-2]
        flat = counts.reshape(-1, self.steps * self.stages).float()
        flat = torch.cat([flat, flat.new_zeros(flat.shape[0], 1)], 1)
        grouped = flat[:, self.cell_index]
        return grouped.reshape(*lead, self.groups, self.max_cells)

    def bucket_groups(self, k: int) -> torch.Tensor:
        return getattr(self, f"bucket_groups_{k}")

    def bucket_cells(self, counts: torch.Tensor) -> list[torch.Tensor]:
        """工步表 [..., S, K] -> 每档一份格子 [..., 本档组数, 本档宽度]，补位为 0。"""
        lead = counts.shape[:-2]
        flat = counts.reshape(-1, self.steps * self.stages).float()
        flat = torch.cat([flat, flat.new_zeros(flat.shape[0], 1)], 1)
        result = []
        for k, (size, width) in enumerate(self.cell_buckets):
            result.append(flat[:, getattr(self, f"bucket_index_{k}")].reshape(*lead, size, width))
        return result

    def unbucket(self, parts: list[torch.Tensor], dim: int) -> torch.Tensor:
        """各档沿组维度的结果拼回原来的组顺序。"""
        return torch.cat(parts, dim).index_select(dim, self.bucket_inverse)

    def equipment(self, features: torch.Tensor) -> torch.Tensor:
        """组表 [..., G, F] -> 空闲 / 停机的整数台数 [..., G, 2]（long）。"""
        return torch.round(features[..., self.c_columns] * self.capacity).long()


class TargetCoder(nn.Module):
    """相邻两帧 -> 离散目标；统计量只用训练集拟合（calibrate）。"""

    def __init__(self, layout: TokenLayout, z_edges=Z_EDGES) -> None:
        super().__init__()
        self.layout = layout
        edges = sorted({float(e) for e in z_edges})
        if not edges or edges[0] <= 0:
            raise ValueError("z_edges must be positive")
        self.register_buffer("edges", torch.tensor([-e for e in reversed(edges)] + edges))
        self.bins = len(edges) * 2 + 1
        g = layout.groups
        # [2, G]：第 0 行 ΔW，第 1 行 ΔQ。
        self.register_buffer("sigma", torch.ones(2, g))
        # 每组每个区间的代表值（训练集里落进该区间的 Δ 的平均，单位 lot）。
        self.register_buffer("bin_values", torch.zeros(2, g, self.bins))
        self.register_buffer("bin_prior", torch.zeros(2, self.bins))           # 全部组合起来的 log 频率
        # C：每组每列所在的档 R [G, 列数]；各列 Δ 的全局 log 频率（−512..512），用于初始化各档的偏置
        self.register_buffer("c_tier", torch.zeros(g, len(C_TARGETS), dtype=torch.long))
        self.register_buffer("c_prior", torch.zeros(len(C_TARGETS), 2 * C_HIST + 1))
        self.register_buffer("calibrated", torch.tensor(False))

    # ---------- 目标 ----------
    def bucket(self, delta: torch.Tensor, which: int) -> torch.Tensor:
        """Δ [..., G] -> 区间编号 [..., G]。"""
        return torch.bucketize(delta / self.sigma[which], self.edges)

    def tiers(self) -> list[tuple[int, int, list[int]]]:
        """(列, 档 R, 属于这一档的组) 的列表；只包含有组的档。"""
        result = []
        for column in range(len(C_TARGETS)):
            for tier in C_TIERS:
                groups = (self.c_tier[:, column] == tier).nonzero().squeeze(1).tolist()
                if groups:
                    result.append((column, tier, groups))
        return result

    @torch.no_grad()
    def targets(self, x_counts, x_features, y_counts, y_features) -> dict:
        layout = self.layout
        w0, q0 = layout.totals(x_counts[:, -1])
        w1, q1 = layout.totals(y_counts)
        c0, c1 = layout.equipment(x_features[:, -1]), layout.equipment(y_features)
        return {
            "w0": w0, "q0": q0, "w1": w1, "q1": q1,
            "w_bin": self.bucket(w1 - w0, 0), "q_bin": self.bucket(q1 - q0, 1),
            "c0": c0, "c1": c1,
            "c1_share": y_features[..., layout.c_columns],
            "cells0": layout.cells(x_counts[:, -1]), "cells1": layout.cells(y_counts),
        }

    def values(self, which: int, bins: torch.Tensor) -> torch.Tensor:
        """区间编号 [B, G] -> 代表值 Δ（lot，未取整）。"""
        table = self.bin_values[which]                                          # [G, bins]
        return table.unsqueeze(0).expand(bins.shape[0], -1, -1).gather(-1, bins.unsqueeze(-1)).squeeze(-1)

    # ---------- 用训练集拟合 ----------
    @torch.no_grad()
    def calibrate(self, dataset) -> dict:
        """dataset: TokenSequenceDataset（训练集）。只用其中作为标签出现的帧。"""
        layout, history = self.layout, dataset.history_len
        deltas, c_deltas = [[], []], []
        for trajectory, segment in zip(dataset.trajectories, dataset.segments):
            start, stop = segment["snapshot_start"] + history, segment["snapshot_stop_exclusive"]
            counts = trajectory.step_counts[start - 1:stop]
            wip, queue = layout.totals(counts)
            deltas[0].append(wip.diff(dim=0))
            deltas[1].append(queue.diff(dim=0))
            equipment = layout.equipment(trajectory.group_features[start - 1:stop])
            c_deltas.append(equipment.diff(dim=0))                               # [N, G, 列数]
        stats = {}
        for which, name in enumerate(("W", "Q")):
            delta = torch.cat(deltas[which])                                    # [N, G]
            sigma = delta.std(0, unbiased=False)
            sigma = torch.where(sigma > 1e-6, sigma, torch.ones_like(sigma))
            self.sigma[which] = sigma
            bins = torch.bucketize(delta / sigma, self.edges)
            # 区间代表值：落进来的 Δ 的平均；空区间用区间中心 × σ（最外两格用 ±8σ）。
            centers = torch.cat([self.edges[:1] - 2.0, (self.edges[1:] + self.edges[:-1]) / 2, self.edges[-1:] + 2.0])
            fallback = centers.unsqueeze(0) * sigma.unsqueeze(1)                 # [G, bins]
            total = torch.zeros(layout.groups, self.bins).scatter_add_(1, bins.T, delta.T)
            count = torch.zeros(layout.groups, self.bins).scatter_add_(1, bins.T, torch.ones_like(delta.T))
            self.bin_values[which] = torch.where(count > 0, total / count.clamp(min=1), fallback)
            frequency = count.sum(0) + 1.0
            self.bin_prior[which] = (frequency / frequency.sum()).log()
            stats[name] = {"sigma": sigma.tolist(), "bin_frequency": (count.sum(0) / count.sum()).tolist()}
        c_delta = torch.cat(c_deltas)                                            # [N, G, 列数]
        largest = c_delta.abs().amax(0).float()                                  # 训练集里的最大 |Δ|
        needed = torch.minimum(torch.ceil(largest * 1.25) + 1, layout.capacity).clamp(min=1)
        tiers = torch.tensor(C_TIERS)
        self.c_tier.copy_(tiers[torch.searchsorted(tiers, needed.long())])
        for column in range(len(C_TARGETS)):
            values = c_delta[..., column].reshape(-1).clamp(-C_HIST, C_HIST) + C_HIST
            frequency = torch.bincount(values, minlength=2 * C_HIST + 1).float() + 1e-3
            self.c_prior[column] = (frequency / frequency.sum()).log()
        self.calibrated.fill_(True)
        tier_counts = {C_TARGETS[c]: {str(r): len(g) for cc, r, g in self.tiers() if cc == c} for c in range(len(C_TARGETS))}
        stats["C"] = {"max_abs_change": largest.tolist(), "tier": self.c_tier.tolist(), "groups_per_tier": tier_counts}
        return {"z_edges": self.edges.tolist(), "bins": self.bins, **stats}
