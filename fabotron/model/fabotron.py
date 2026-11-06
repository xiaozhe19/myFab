"""Fabotron：输入两张表，输出下一帧的离散分布。

    工步表 ─► 格子 embedding（按组内比例加权求和）─┐
    组特征 ─► Linear ─────────────────────────────┼─► + 组 embedding ─► S-T-S 主干 ─► h（最后一帧）
                                                   │
    h ─► C 头（空闲 / 停机台数的 Δ，整数分类；按变化幅度分档，每档一个头）
         两列并行、互不为条件（真实 Δ 相关只有 −0.1~−0.2）；忙 = 总 − 空闲 − 停机。
         抽样后若空闲 + 停机 > 总台数，把空闲截到 总 − 停机
      ─► + C 条件 ─► W 头（ΔW 区间）
      ─► + W 区间 embedding ─► Q 头（ΔQ 区间）
      ─► + Q 区间 embedding ─► 组内比例（h·格子向量，组内 softmax）

训练时用真实值做条件（teacher forcing），四个头一次并行算完；``sample`` 逐个抽样。

C 头的分档由训练集决定，所以先 ``TargetCoder.calibrate``，再用它构造模型：

    coder = TargetCoder(TokenLayout(schema)); coder.calibrate(train_set)
    model = Fabotron(schema, coder, ...); model.init_from_coder()
"""

import torch
from torch import nn

from fabotron.layout import C_HIST, C_TARGETS, TargetCoder, TokenLayout
from fabotron.model.spatial_transformer_block import SpatialTransformerBlock
from fabotron.model.temporal_transformer_block import TemporalTransformerBlock

MASKED = -1e9


def head(d_model, out_dim):
    return nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_model), nn.GELU(), nn.Linear(d_model, out_dim))


class Fabotron(nn.Module):
    def __init__(self, schema, coder, d_model=64, ff_mult=4, dropout=0.1, route_bias=None, c_hidden=None):
        super().__init__()
        if not bool(coder.calibrated):
            raise ValueError("calibrate the TargetCoder before building the model (C tiers come from data)")
        self.coder, self.layout = coder, coder.layout
        layout = self.layout

        # ---- 输入编码 ----
        # 每个格子一个向量，按格子数分档存放：每档 [本档组数, 本档宽度, d]（见 TokenLayout.cell_buckets），
        # 直接作参数用、不按下标取，反向传播快（补位格的输入比例恒为 0、输出被 mask，梯度恒为 0）
        def cell_params(*tail, scale):
            return nn.ParameterList(nn.Parameter(torch.randn(size, width, *tail) * scale)
                                    for size, width in layout.cell_buckets)
        self.cell_in = cell_params(d_model, scale=0.02)
        # 组特征：W/100、Q/100、log1p(W)、log1p(Q) + 组表（去掉恒为 1 的 total_tools）
        self.feature_proj = nn.Linear(4 + layout.features - 1, d_model)
        self.group_embedding = nn.Parameter(torch.zeros(layout.groups, d_model))
        keep = [i for i in range(layout.features) if i != layout.total_tools_column]
        self.register_buffer("feature_keep", torch.tensor(keep), persistent=False)

        # ---- 主干（与之前相同）----
        self.spatial_before = SpatialTransformerBlock(d_model, ff_mult=ff_mult, dropout=dropout)
        self.temporal = TemporalTransformerBlock(d_model, ff_mult=ff_mult, dropout=dropout)
        self.spatial_after = SpatialTransformerBlock(d_model, ff_mult=ff_mult, dropout=dropout)
        self.register_buffer("route_bias", None if route_bias is None else route_bias.detach().clone())

        # ---- 输出头：C -> W -> Q -> 组内比例 ----
        # C：每个 (列, 档) 一个头，只算属于这一档的组。
        # 各头结构相同（LayerNorm → Linear → GELU → Linear），参数叠成 [档数, ...] 一次算完：
        # 每档的组补到最多组数、类别补到最多类别数（补位的结果切掉不用），省掉 19 次逐个调用
        tiers = coder.tiers()
        self.c_tiers = [(column, tier) for column, tier, _ in tiers]
        for k, (_, _, groups) in enumerate(tiers):
            self.register_buffer(f"c_groups_{k}", torch.tensor(groups), persistent=False)
        self.c_sizes = [len(groups) for _, _, groups in tiers]
        n_tiers, max_groups = len(tiers), max(self.c_sizes)
        max_classes = 2 * max(tier for _, tier in self.c_tiers) + 1
        # 每档每个位置取 h 的哪一行（h 按 [列, 组] 摊平），补位取第 0 行
        index = torch.zeros(n_tiers, max_groups, dtype=torch.long)
        for k, (column, _, groups) in enumerate(tiers):
            index[k, :len(groups)] = column * layout.groups + torch.tensor(groups)
        self.register_buffer("c_index", index, persistent=False)
        offsets = torch.arange(max_classes) - torch.tensor([tier for _, tier in self.c_tiers])[:, None]
        self.register_buffer("c_offsets", offsets, persistent=False)            # 类别 -> Δ，[档数, 最多类别]
        c_hidden = c_hidden or d_model                                           # C 头隐藏层宽度，默认 = d_model
        bound = d_model ** -0.5                                                  # 同 nn.Linear 的默认初始化范围
        self.c_norm_weight = nn.Parameter(torch.ones(n_tiers, 1, d_model))
        self.c_norm_bias = nn.Parameter(torch.zeros(n_tiers, 1, d_model))
        self.c_weight1 = nn.Parameter(torch.empty(n_tiers, d_model, c_hidden).uniform_(-bound, bound))
        self.c_bias1 = nn.Parameter(torch.empty(n_tiers, 1, c_hidden).uniform_(-bound, bound))
        self.c_weight2 = nn.Parameter(torch.empty(n_tiers, c_hidden, max_classes).uniform_(-c_hidden ** -0.5, c_hidden ** -0.5))
        self.c_bias2 = nn.Parameter(torch.empty(n_tiers, 1, max_classes).uniform_(-c_hidden ** -0.5, c_hidden ** -0.5))
        self.c_condition = nn.Linear(len(C_TARGETS), d_model)
        self.w_head = head(d_model, self.coder.bins)
        self.w_embedding = nn.Embedding(self.coder.bins, d_model)
        self.q_head = head(d_model, self.coder.bins)
        self.q_embedding = nn.Embedding(self.coder.bins, d_model)
        self.comp_proj = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_model))
        self.cell_out = cell_params(d_model, scale=0.02)
        self.cell_bias = cell_params(scale=0.0)
        # 照抄上一帧比例的强度；初值 1 = 一开始就接近"抄上一个"
        self.persistence_weight = nn.Parameter(torch.ones(()))

    # ---------- 初始化：先验分布 ----------
    @torch.no_grad()
    def init_from_coder(self):
        """末层权重置零、偏置 = 训练集先验 log 频率：初始输出就是边际分布。"""
        if not bool(self.coder.calibrated):
            raise ValueError("calibrate the TargetCoder first")
        self.c_weight2.zero_()
        self.c_bias2.zero_()
        for k, (column, tier) in enumerate(self.c_tiers):
            self.c_bias2[k, 0, :2 * tier + 1] = self.coder.c_prior[column, C_HIST - tier:C_HIST + tier + 1].log_softmax(-1)
        pairs = [(self.w_head, self.coder.bin_prior[0]), (self.q_head, self.coder.bin_prior[1])]
        for module, prior in pairs:
            nn.init.zeros_(module[-1].weight)
            module[-1].bias.copy_(prior)

    # ---------- 编码 ----------
    def encode(self, x_counts, x_features):
        """x_counts [B,T,S,K] int、x_features [B,T,G,F] -> 最后一帧每组隐向量 [B,G,d]。"""
        layout = self.layout
        wip, queue = layout.totals(x_counts)                                     # [B,T,G]
        parts = []
        for k, (cells, weight) in enumerate(zip(layout.bucket_cells(x_counts), self.cell_in)):
            share = cells / wip[..., layout.bucket_groups(k)].clamp(min=1).unsqueeze(-1)   # 组内比例 [B,T,g,c]
            parts.append(torch.einsum("btgc,gcd->btgd", share, weight))
        composition = layout.unbucket(parts, 2)
        scalars = torch.stack([wip / layout.wip_scale, queue / layout.queue_scale,
                               wip.log1p(), queue.log1p()], -1)
        features = torch.cat([scalars, x_features[..., self.feature_keep]], -1)
        hidden = self.feature_proj(features) + composition + self.group_embedding
        hidden, _ = self.spatial_before(hidden, route_bias=self.route_bias)
        hidden, _ = self.temporal(hidden)
        # 只用最后一帧：spatial_after 是逐帧的（组间 attention + FFN），前几帧算了也不用
        hidden, _ = self.spatial_after(hidden[:, -1], route_bias=self.route_bias)
        return hidden

    # ---------- 各个头 ----------
    def c_groups(self, k):
        return getattr(self, f"c_groups_{k}")

    def c_heads(self, h_columns, c0, ks):
        """档 ks（连续切片）的 logits 列表，每个 [B, 该档组数, 2R+1]；下一帧个数不在 [0, 容量] 内的类别被 mask。

        h_columns [B, 列数, G, d]：每列各自的隐向量（顺序 C 下后面的列带着前面列的条件）。
        """
        index = self.c_index[ks]                                                 # [K, 最多组数]
        x = h_columns.flatten(1, 2)[:, index]                                    # [B, K, 最多组数, d]
        x = nn.functional.layer_norm(x, x.shape[-1:]) * self.c_norm_weight[ks] + self.c_norm_bias[ks]
        x = nn.functional.gelu(torch.einsum("bkgd,kde->bkge", x, self.c_weight1[ks]) + self.c_bias1[ks])
        logits = torch.einsum("bkgd,kde->bkge", x, self.c_weight2[ks]) + self.c_bias2[ks]
        after = c0.transpose(1, 2).flatten(1)[:, index, None] + self.c_offsets[ks, None]
        capacity = self.layout.capacity.transpose(0, 1).flatten()[index, None]
        logits = logits.masked_fill((after < 0) | (after > capacity), MASKED)
        # 先 unbind 再切：直接逐档切大张量，反向时每档都要建一个整块大小的零张量再累加
        return [part[:, :self.c_sizes[k], :2 * self.c_tiers[k][1] + 1]
                for part, k in zip(logits.unbind(1), range(len(self.c_tiers))[ks])]

    def c_logits(self, h, c0, draw=None):
        """全部 (列, 档) 的 logits（顺序同 c_tiers），各列都直接读 h、互不为条件。

        draw 给定时顺带按 logits 抽样。返回 (logits 列表, 抽到的 Δ 列表或 None)。
        """
        h_columns = h.unsqueeze(1).expand(-1, len(C_TARGETS), -1, -1)
        logits = self.c_heads(h_columns, c0, slice(None))
        if draw is None:
            return logits, None
        return logits, [draw(value) - tier for value, (_, tier) in zip(logits, self.c_tiers)]

    def c_classes(self, c0, c1):
        """真实的 C 变化 -> 各档的类别编号；超出本档范围的归到最外一格。"""
        return [(c1 - c0)[:, self.c_groups(k), column].clamp(-tier, tier) + tier
                for k, (column, tier) in enumerate(self.c_tiers)]

    def c_apply(self, c0, values):
        """各档的 Δ（[B, 该档组数]）写回 [B, G, 3]。"""
        result = c0.clone().to(values[0].dtype if values else c0.dtype)
        for k, ((column, _), delta) in enumerate(zip(self.c_tiers, values)):
            groups = self.c_groups(k)
            result[:, groups, column] = c0[:, groups, column].to(result.dtype) + delta
        return result

    def c_feasible(self, c1):
        """两列独立抽样可能 空闲 + 停机 > 总台数：把空闲截到 总 − 停机（停机更像外生事件，保留）。"""
        idle, down = C_TARGETS.index("idle_tools"), C_TARGETS.index("down_tools")
        total = self.layout.capacity[:, idle].to(c1.dtype)
        result = c1.clone()
        result[..., idle] = torch.minimum(c1[..., idle], total - c1[..., down])
        return result

    def comp_logits(self, h, cells0):
        layout = self.layout
        projected = self.comp_proj(h)
        parts = []
        for k, (weight, bias) in enumerate(zip(self.cell_out, self.cell_bias)):
            part = torch.einsum("bgd,gcd->bgc", projected[:, layout.bucket_groups(k)], weight) + bias
            parts.append(nn.functional.pad(part, (0, layout.max_cells - part.shape[-1]), value=MASKED))
        logits = layout.unbucket(parts, 1)                                       # [B, G, 最大格子数]
        previous = cells0 / cells0.sum(-1, keepdim=True).clamp(min=1)
        logits = logits + self.persistence_weight * (previous + 1e-3).log()
        return logits.masked_fill(~layout.cell_mask, MASKED)

    def forward(self, x_counts, x_features, targets):
        """Teacher forcing：条件都用 targets 里的真实值。返回各头的 logits。"""
        h = self.encode(x_counts, x_features)
        c, _ = self.c_logits(h, targets["c0"])
        c_class = self.c_classes(targets["c0"], targets["c1"])
        h = h + self.c_condition(targets["c1_share"])
        w = self.w_head(h)
        h = h + self.w_embedding(targets["w_bin"])
        q = self.q_head(h)
        h = h + self.q_embedding(targets["q_bin"])
        return {"c": c, "c_class": c_class, "w": w, "q": q, "comp": self.comp_logits(h, targets["cells0"])}

    def _ancestral(self, h, c0, cells0, generator=None):
        """组内逐个抽样：C -> W|C -> Q|C,W -> 组内比例。返回各头的 logits 和抽到的值。"""
        layout, coder = self.layout, self.coder

        def draw(logits):
            probs = logits.softmax(-1)
            flat = torch.multinomial(probs.reshape(-1, probs.shape[-1]), 1, generator=generator)
            return flat.reshape(probs.shape[:-1])

        c, deltas = self.c_logits(h, c0, draw=draw)
        c1 = self.c_feasible(self.c_apply(c0, deltas))
        h = h + self.c_condition(c1 / layout.capacity)
        w = self.w_head(h)
        w_bin = draw(w)
        h = h + self.w_embedding(w_bin)
        q = self.q_head(h)
        q_bin = draw(q)
        h = h + self.q_embedding(q_bin)
        logits = {"c": c, "w": w, "q": q, "comp": self.comp_logits(h, cells0)}
        return logits, {"c1": c1, "w_bin": w_bin, "q_bin": q_bin}

    @torch.no_grad()
    def sample(self, x_counts, x_features, generator=None):
        """抽一份下一帧：组级数值（已修成合法值）和组内比例。"""
        layout, coder = self.layout, self.coder
        w0, q0 = layout.totals(x_counts[:, -1])
        logits, drawn = self._ancestral(self.encode(x_counts, x_features), layout.equipment(x_features[:, -1]),
                                        layout.cells(x_counts[:, -1]), generator)
        w1 = (w0 + coder.values(0, drawn["w_bin"]).round()).clamp(min=0)
        q1 = torch.minimum((q0 + coder.values(1, drawn["q_bin"]).round()).clamp(min=0), w1)
        return {"w1": w1, "q1": q1, **drawn, "composition": logits["comp"].softmax(-1)}

    @torch.no_grad()
    def predict(self, x_counts, x_features, samples=8, generator=None):
        """单步边际分布：沿 C->W->Q 链抽 samples 次，把各头的概率取平均。

        训练时 W 头看的是真实 C、Q 头看的是真实 ΔW；评估点估计时不能这样（等于泄漏答案），
        要像部署时一样用自己抽出来的值做条件，再对抽样取平均。
        """
        layout = self.layout
        batch = x_counts.shape[0]
        c0_single = layout.equipment(x_features[:, -1])
        h = self.encode(x_counts, x_features).repeat(samples, 1, 1)
        c0 = c0_single.repeat(samples, 1, 1)
        cells0 = layout.cells(x_counts[:, -1]).repeat(samples, 1, 1)
        logits, _ = self._ancestral(h, c0, cells0, generator)

        def average(value):
            probs = value.softmax(-1)
            return probs.reshape(samples, batch, *probs.shape[1:]).mean(0)

        # C 的期望个数：各档 Σ p·Δ 写回 [B, G, 3]
        expected = [(average(value) * torch.arange(-tier, tier + 1, device=c0.device)).sum(-1)
                    for value, (_, tier) in zip(logits["c"], self.c_tiers)]
        return {"c_hat": self.c_apply(c0_single.float(), expected),
                **{key: average(logits[key]) for key in ("w", "q", "comp")}}

    @classmethod
    def from_checkpoint(cls, checkpoint, route_bias=None):
        """从 train.py 保存的 checkpoint 重建：先按保存的分档建 coder，再建模型。"""
        coder = TargetCoder(TokenLayout(checkpoint["schema"]))
        state = checkpoint["model_state"]
        coder.load_state_dict({key[len("coder."):]: value for key, value in state.items() if key.startswith("coder.")})
        if route_bias is None:
            route_bias = state.get("route_bias")
        model = cls(checkpoint["schema"], coder, **checkpoint["model_config"], route_bias=route_bias)
        model.load_state_dict(state)
        return model
