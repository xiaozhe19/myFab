from torch import nn

from fabotron.model.temporal_attention import TemporalSelfAttention


class TemporalTransformerBlock(nn.Module):
    def __init__(
        self,
        d_model,
        ff_mult=4,
        dropout=0.1,
        rope_base=10000.0,
    ):
        super().__init__()

        self.norm1 = nn.LayerNorm(d_model)
        self.attention = TemporalSelfAttention(
            d_model,
            rope_base=rope_base,
        )
        self.out_proj = nn.Linear(d_model, d_model)
        self.dropout1 = nn.Dropout(dropout)

        self.norm2 = nn.LayerNorm(d_model)

        ff_dim = d_model * ff_mult
        self.ffn = nn.Sequential(
            nn.Linear(d_model, ff_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(ff_dim, d_model),
        )
        self.dropout2 = nn.Dropout(dropout)

    def forward(self, x):
        normalized = self.norm1(x)
        attended, weights = self.attention(normalized)

        attended = self.out_proj(attended)
        x = x + self.dropout1(attended)

        transformed = self.ffn(self.norm2(x))
        x = x + self.dropout2(transformed)

        return x, weights