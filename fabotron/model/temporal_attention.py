"""
时间Attention
时间按照RoPE编码
"""
import torch
from torch import nn

class TemporalSelfAttention(nn.Module):
    def __init__(self,d_model, rope_base = 10000.0):
        super().__init__()

        if d_model <= 0 or d_model % 2 != 0:
            raise ValueError("d_model 必须是正偶数")

        self.q_proj = nn.Linear(d_model, d_model)
        self.k_proj = nn.Linear(d_model,d_model)
        self.v_proj = nn.Linear(d_model,d_model)


        inv_freq = rope_base ** (
            -torch.arange(0, d_model, 2, dtype=torch.float32)
            / d_model
        )

        self.register_buffer("inv_freq",inv_freq)

    def _apply_rope(self,x):
        # x: [B,G,T,D]

        steps = x.shape[-2]
        position = torch.arange(
            steps,
            device=x.device,
            dtype=self.inv_freq.dtype,
        )

        angles = position[:, None] * self.inv_freq[None, :]
        cos = angles.cos().to(x.dtype)[None, None,:,:]
        sin = angles.sin().to(x.dtype)[None,None,:,:]

        even = x[...,0::2]
        odd = x[...,1::2]

        rotated_even = even * cos - odd *sin
        rotated_odd = even *sin + odd * cos

        return torch.stack(
            (rotated_even, rotated_odd),
            dim=-1,
        ).flatten(-2)

    def forward(self,x):
        x = x.transpose(1,2)

        q = self._apply_rope(self.q_proj(x))
        k = self._apply_rope(self.k_proj(x))
        v = self.v_proj(x)

        scores = q @ k.transpose(-2,-1)
        scores = scores / (q.shape[-1]**0.5)

        steps = x.shape[-2]
        future_mask = torch.ones(
            steps,
            steps,
            device=x.device,
            dtype = torch.bool,
        ).triu(diagonal=1)

        scores = scores.masked_fill(
            future_mask,
            float("-inf")
        )

        weights = torch.softmax(scores,dim=-1)
        output = weights @ v

        return output.transpose(1,2), weights