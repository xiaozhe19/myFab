"""Candidate state losses. Context/controls and static capacity are not targets."""

from __future__ import annotations

import math

import torch
from torch import nn


PARTS = ("W", "Q", "P_WIP", "P_Queue", "E")


class TokenStateLoss(nn.Module):
    def __init__(self, schema, mode="v1", smooth_count=1.0, weights=None, scales=None):
        super().__init__()
        if mode not in ("v1", "block-mse") or not math.isfinite(smooth_count) or smooth_count <= 0:
            raise ValueError("Invalid loss mode or count smoothing constant")
        self.schema, self.mode, self.smooth_count = schema, mode, smooth_count
        self.slices = schema["slices"]
        self.wip_scale = float(schema["normalization"]["W"])
        self.queue_scale = float(schema["normalization"]["Q"])
        if any(not math.isfinite(v) or v <= 0 for v in (self.wip_scale, self.queue_scale)):
            raise ValueError("Tokenizer count scales must be finite and positive")
        if any(self.slices[name][1] - self.slices[name][0] != 1 for name in ("W", "Q")):
            raise ValueError("W and Q must each contain one scalar")
        columns = schema["C_columns"]
        start, stop = self.slices["C"]
        if stop - start != len(columns):
            raise ValueError("C_columns do not match the equipment slice")
        self.equipment_indices = [start + columns.index(name) for name in
                                  ("available_tools", "occupied_slots", "down_tools")]
        self.weights = {"W": 1.0, "Q": 1.0, "P": 1.0, "E": 1.0}
        if weights:
            if set(weights) - set(self.weights):
                raise ValueError("Unknown loss weights")
            self.weights.update(weights)
        if any(not math.isfinite(v) or v < 0 for v in self.weights.values()) or not sum(self.weights.values()):
            raise ValueError("Loss weights must be finite, nonnegative, and not all zero")
        self.scales = {name: 1.0 for name in PARTS}
        self.normalization = "none"
        if scales is not None:
            if set(scales) != set(PARTS) or any(not math.isfinite(v) or v <= 0 for v in scales.values()):
                raise ValueError("Invalid fitted loss scales")
            self.scales.update(scales)
            self.normalization = "provided_scales"

    def block(self, token, name):
        start, end = self.slices[name]
        return token[..., start:end]

    def state(self, token):
        wip = self.block(token, "W") * self.wip_scale
        queue = self.block(token, "Q") * self.queue_scale
        return {
            "W": wip, "Q": queue,
            "P_WIP": wip * self.block(token, "P_WIP"),
            "P_Queue": queue * self.block(token, "P_Queue"),
            "E": token[..., self.equipment_indices],
        }

    def components(self, prediction, target):
        if prediction.shape != target.shape or prediction.shape[-1] != self.schema["input_dim"]:
            raise ValueError("Prediction and complete target-token shapes must agree")
        if self.mode == "block-mse":
            result = {name: (self.block(prediction, name) - self.block(target, name)).square().mean()
                      for name in PARTS if name != "E"}
            result["E"] = (prediction[..., self.equipment_indices] - target[..., self.equipment_indices]).square().mean()
            return result
        predicted, actual = self.state(prediction), self.state(target)
        return {
            "W": ((predicted["W"] - actual["W"]).square() / (actual["W"] + self.smooth_count)).mean(),
            "Q": ((predicted["Q"] - actual["Q"]).square() / (actual["Q"] + self.smooth_count)).mean(),
            "P_WIP": (predicted["P_WIP"] - actual["P_WIP"]).abs().mean(),
            "P_Queue": (predicted["P_Queue"] - actual["P_Queue"]).abs().mean(),
            "E": (predicted["E"] - actual["E"]).square().mean(),
        }

    def combine(self, parts):
        normalized = {name: parts[name] / self.scales[name] for name in PARTS}
        if self.mode == "v1":
            return (self.weights["W"] * normalized["W"] + self.weights["Q"] * normalized["Q"]
                    + 0.5 * self.weights["P"] * (normalized["P_WIP"] + normalized["P_Queue"])
                    + self.weights["E"] * normalized["E"])
        weights = {"W": self.weights["W"], "Q": self.weights["Q"],
                   "P_WIP": self.weights["P"], "P_Queue": self.weights["P"], "E": self.weights["E"]}
        return sum(weights[name] * normalized[name] for name in PARTS) / sum(weights.values())

    def forward(self, prediction, target):
        return self.combine(self.components(prediction, target))

    @torch.no_grad()
    def calibrate(self, training_dataset):
        """Use only training labels; zero-state reference defines each typical loss scale.

        For block-mse these are training target mean squares (squared RMS).
        For v1 they have the units of their own weighted-MSE or count-MAE term.
        """
        totals = {name: 0.0 for name in PARTS}
        count = 0
        for index, segment in enumerate(training_dataset.segments):
            start = segment["snapshot_start"] + training_dataset.history_len
            stop = segment["snapshot_stop_exclusive"]
            labels = training_dataset.trajectories[index].tokens[start:stop]
            for target in labels.split(32):
                parts = self.components(torch.zeros_like(target), target)
                for name in PARTS:
                    totals[name] += parts[name].item() * len(target)
                count += len(target)
        if not count:
            raise ValueError("Cannot calibrate using an empty training set")
        for name in PARTS:
            scale = totals[name] / count
            self.scales[name] = scale if scale > 1e-8 else 1.0
        self.normalization = "training_zero_reference"
        return self.configuration()

    def configuration(self):
        return {"mode": self.mode, "smooth_count_lots": self.smooth_count,
                "weights": dict(self.weights), "scales": dict(self.scales),
                "normalization": self.normalization,
                "scale_definition": "Mean zero-state reference loss using training labels only; <=1e-8 uses 1",
                "conservation_weight": 0.0,
                "ignored_outputs": ["M", "B", "policy", "C.total_tools"],
                "distribution_definition": (
                    "Per-category Lot MAE; WIP and Queue normalized separately then averaged"
                    if self.mode == "v1" else "MSE of Tokenizer proportion columns, each block scaled separately")}


class StateMetrics:
    def __init__(self, objective):
        self.objective = objective
        self.count = 0
        self.parts = {name: 0.0 for name in PARTS}
        self.metrics = {}
        self.tv_sums = {"W": 0.0, "Q": 0.0}
        self.tv_counts = {"W": 0, "Q": 0}

    @torch.no_grad()
    def update(self, prediction, target):
        size = len(target)
        objective = self.objective
        for name, value in objective.components(prediction, target).items():
            self.parts[name] += value.item() * size
        p, y = objective.state(prediction), objective.state(target)
        values = {
            "wip_mae_lots": (p["W"] - y["W"]).abs().mean(),
            "queue_mae_lots": (p["Q"] - y["Q"]).abs().mean(),
            "wip_category_l1_lots_per_group": (p["P_WIP"] - y["P_WIP"]).abs().sum(-1).mean(),
            "queue_category_l1_lots_per_group": (p["P_Queue"] - y["P_Queue"]).abs().sum(-1).mean(),
            "equipment_mse": (p["E"] - y["E"]).square().mean(),
            "negative_wip_fraction": (p["W"] < 0).float().mean(),
            "negative_queue_fraction": (p["Q"] < 0).float().mean(),
            "queue_exceeds_wip_fraction": (p["Q"] > p["W"]).float().mean(),
        }
        for name, value in values.items():
            self.metrics[name] = self.metrics.get(name, 0.0) + value.item() * size
        for total, block in (("W", "P_WIP"), ("Q", "P_Queue")):
            raw = objective.block(prediction, block)
            actual = objective.block(target, block)
            positive = raw.clamp_min(0)
            mass = positive.sum(-1, keepdim=True)
            projected = torch.where(mass > 0, positive / mass.clamp_min(1e-12), torch.full_like(raw, 1 / raw.shape[-1]))
            valid = y[total].squeeze(-1) > 0
            tv = 0.5 * (projected - actual).abs().sum(-1)
            self.tv_sums[total] += tv[valid].sum().item()
            self.tv_counts[total] += valid.sum().item()
            key = block + "_raw_mass_mae"
            value = (raw.sum(-1) - actual.sum(-1)).abs().mean().item()
            self.metrics[key] = self.metrics.get(key, 0.0) + value * size
        self.count += size

    def result(self):
        if not self.count:
            raise ValueError("Empty evaluation set")
        parts = {name: value / self.count for name, value in self.parts.items()}
        result = {"samples": self.count, "loss": self.objective.combine(parts), "components": parts,
                  **{name: value / self.count for name, value in self.metrics.items()}}
        for total, name in (("W", "wip"), ("Q", "queue")):
            result[name + "_composition_tv_projected"] = (
                self.tv_sums[total] / self.tv_counts[total] if self.tv_counts[total] else None)
        return result
