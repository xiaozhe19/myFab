import torch
from torch import nn

class SpatialQKV(nn.Module):
    def __init__(self,input_dim,attention_dim=64):
        super().__init__()

        self.q_proj = nn.Linear(input_dim, attention_dim)
        self.k_proj = nn.Linear(input_dim,attention_dim)
        self.v_proj = nn.Linear(input_dim,attention_dim)

    def forward(self,x):
        q = self.q_proj(x)
        k = self.k_proj(x)
        v = self.v_proj(x)

        return q,k,v


class SpatialSelfAttention(nn.Module):
    def __init__(self, input_dim, attention_dim):
        super().__init__()

        self.qkv = SpatialQKV(input_dim, attention_dim)

    def forward(self, x, route_bias = None,beta = 1):
        q,k,v = self.qkv(x)

        scores = q @ k.transpose(-2,-1)
        scores = scores / (q.shape[-1] ** 0.5)


        if route_bias is not None:
            scores = scores + route_bias * beta

        weights = torch.softmax(scores, dim = -1)

        output = weights @ v

        return output, weights
