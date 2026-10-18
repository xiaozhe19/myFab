import torch

from fab.model.entities import FabModel

def build_route_counts(model:FabModel,group_ids):
    group_to_index = {
        group_id: index
        for index, group_id in enumerate(group_ids)
    }

    num_groups = len(group_ids)

    counts = torch.zeros(
        (num_groups, num_groups),
        dtype=torch.float32,
    )

    for product in model.products.values():
        route = product.route

        for current_step, next_step in zip(route, route[1:]):
            src = group_to_index[current_step.tool_group_id]
            dst = group_to_index[next_step.tool_group_id]

            counts[src,dst] +=1

    return counts

def build_route_bias(model:FabModel,group_ids):
    counts = build_route_counts(model,group_ids)

    bias = torch.log1p(counts.transpose(-2,-1))
    scale = bias.max().clamp_min(1e-8)

    return bias/scale
