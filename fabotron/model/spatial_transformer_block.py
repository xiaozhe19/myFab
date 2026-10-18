
"""
一个空间Transformer层
$$U=X+\operatorname{Dropout}\left(\operatorname{AttentionOut}(\operatorname{LN}_1(X),B)\right)$$
$$Y=U+\operatorname{Dropout}\left(\operatorname{FFN}(\operatorname{LN}_2(U))\right)$$
"""

from torch import nn

from fabotron.model.spatial_attention import SpatialSelfAttention


class SpatialTransformerBlock(nn.Module):
    def __init__(self, d_model,ff_mult = 4, dropout= 0.1):
        super().__init__()

        self.norm1 = nn.LayerNorm(d_model)
        self.attention = SpatialSelfAttention(d_model,d_model)
        self.out_proj = nn.Linear(d_model,d_model)
        self.dropout1 = nn.Dropout(dropout)

        self.norm2 = nn.LayerNorm(d_model)

        ff_dim = d_model * ff_mult
        self.ffn = nn.Sequential(
            nn.Linear(d_model, ff_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(ff_dim, d_model)
        )

        self.dropout2 = nn.Dropout(dropout)

    def forward(self, x, route_bias = None, beta = 1.0):
        normalized = self.norm1(x)
        attended, weights = self.attention(
            normalized,
            route_bias = route_bias,
            beta = beta,
        )

        attended = self.out_proj(attended)
        x = x + self.dropout1(attended)

        transformed = self.ffn(self.norm2(x))
        x = x + self.dropout2(transformed)

        return x, weights