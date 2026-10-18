from torch import nn

from fabotron.model.spatial_transformer_block import SpatialTransformerBlock
from fabotron.model.temporal_transformer_block import TemporalTransformerBlock


class Fabotron(nn.Module):
    def __init__(self,
                 input_dim,
                 d_model = 64,
                 ff_mult = 4,
                 dropout = 0.1,
                 route_bias = None
                 ):
        super().__init__()

        self.input_proj = nn.Linear(input_dim, d_model)

        self.spatial_before = SpatialTransformerBlock(
            d_model,
            ff_mult=ff_mult,
            dropout=dropout
        )

        self.temporal = TemporalTransformerBlock(
            d_model,
            ff_mult=ff_mult,
            dropout=dropout
        )

        self.spatial_after = SpatialTransformerBlock(
            d_model,
            ff_mult=ff_mult,
            dropout=dropout
        )

        self.output_head = nn.Linear(d_model,input_dim)

        self.register_buffer(
            "route_bias",
            None if route_bias is None else route_bias.detach().clone(),
        )

    def forward(self,x):
        hidden = self.input_proj(x)

        hidden, _ = self.spatial_before(
            hidden,
            route_bias = self.route_bias,
        )

        hidden,_ = self.temporal(hidden)

        hidden, _ = self.spatial_after(
            hidden,
            route_bias = self.route_bias
        )
        last_hidden = hidden[:,-1]
        prediction = self.output_head(last_hidden)

        return prediction