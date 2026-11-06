"""Train the S-T-S distributional predictor (Token V2) from Tokenizer SQLite snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import time

os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader

from fabotron.dataset import load_collection_datasets, load_trajectories, make_datasets
from fabotron.loss import FabotronLoss, StateMetrics
from fabotron.layout import TargetCoder, TokenLayout
from fabotron.model.fabotron import Fabotron


ROOT = Path(__file__).resolve().parents[1]
HK_TIME = timezone(timedelta(hours=8))
EVAL_INTERVAL = 10   # 每 10 个 epoch 做一次完整抽样评估（其余 epoch 只算验证 loss）


def to_device(batch, device):
    *tables, indices = batch
    return [t.to(device, non_blocking=True) for t in tables], indices


def train_one_batch(model, tables, optimizer, objective, grad_clip=1.0):
    model.train()
    optimizer.zero_grad(set_to_none=True)
    x_counts, x_features, _, _ = tables
    targets = model.coder.targets(*tables)
    loss, _ = objective(model(x_counts, x_features, targets), targets)
    if not torch.isfinite(loss):
        raise RuntimeError("Non-finite training loss")
    loss.backward()
    if grad_clip > 0:
        nn.utils.clip_grad_norm_(model.parameters(), grad_clip, error_if_nonfinite=True)
    optimizer.step()
    return loss.detach()


@torch.no_grad()
def evaluate_loss(model, loader, objective, device):
    """Teacher-forcing loss only, with the same batch weighting as full evaluation."""
    model.eval()
    sums, count = {}, 0
    for batch in loader:
        tables, _ = to_device(batch, device)
        targets = model.coder.targets(*tables)
        outputs = model(tables[0], tables[1], targets)
        if not torch.stack([torch.isfinite(v).all() for v in [outputs["w"], outputs["q"], *outputs["c"]]]).all():
            raise RuntimeError("Non-finite logits")
        size = len(tables[0])
        for name, value in objective.components(outputs, targets).items():
            sums[name] = sums.get(name, 0.0) + value.double() * size
        count += size
    if not count:
        raise ValueError("Empty evaluation set")
    parts = {name: float(value / count) for name, value in sums.items()}
    return {"samples": count, "components": parts, "loss": float(objective.combine(parts))}


@torch.no_grad()
def evaluate(model, loader, objective, device, trajectories, persistence=False, samples=8, seed=0):
    """persistence=True: 下一帧 = 上一帧，在完全相同的样本上算点估计指标。

    模型：联合 NLL 用 teacher forcing；点估计用 ``model.predict`` 的边际分布（抽 samples 次，
    固定随机种子，所以每个 epoch 的评估可比）。
    """
    model.eval()
    generator = torch.Generator(device=device).manual_seed(seed)
    coder = model.coder
    metric_objective = None if persistence else objective
    total = StateMetrics(coder, metric_objective)
    separate = {index: StateMetrics(coder, metric_objective) for index in range(len(trajectories))}
    for batch in loader:
        tables, indices = to_device(batch, device)
        targets = coder.targets(*tables)
        outputs = probs = None
        if not persistence:
            outputs = model(tables[0], tables[1], targets)
            if not torch.stack([torch.isfinite(v).all() for v in [outputs["w"], outputs["q"], *outputs["c"]]]).all():
                raise RuntimeError("Non-finite logits")
            probs = model.predict(tables[0], tables[1], samples, generator)
        total.update(targets, probs, outputs)
        for index in indices.unique().tolist():
            mask = (indices == index).to(device)
            sub_targets = {key: value[mask] for key, value in targets.items()}
            def pick(result):
                if result is None:
                    return None
                return {key: [v[mask] for v in value] if isinstance(value, list) else value[mask]
                        for key, value in result.items()}
            separate[index].update(sub_targets, pick(probs), pick(outputs))
    result = total.result()
    result["per_trajectory"] = [
        {"name": trajectories[index].name, "database": str(trajectories[index].database),
         "run_id": trajectories[index].run_id, **metrics.result()}
        for index, metrics in separate.items() if metrics.count
    ]
    return result


def build_bias(model_database, trajectory):
    from fab.model.sqlite_loader import load_fab_model_from_sqlite
    from fab.plugins.tokenizer_core import TokenizerConfig, ToolGroupTokenizer
    from fabotron.model.route_bias import build_route_bias

    schema = trajectory.schema
    model = load_fab_model_from_sqlite(model_database)
    config = TokenizerConfig(
        processing_edges_minutes=schema["processing_edges_minutes"],
        downtime_ratio_edges=schema["downtime_ratio_edges"],
        dispatch_rules=schema["dispatch_rules"], horizon_minutes=schema["horizon_minutes"],
        wip_scale=schema["normalization"]["W"], queue_scale=schema["normalization"]["Q"],
    )
    recipe_mapping = None
    if schema["recipe_source"] == "explicit_static_mapping":
        recipe_mapping = {(p, step): recipe for p, step, group, recipe, visit in schema["steps"]}
    tokenizer = ToolGroupTokenizer(model, config, recipe_by_step=recipe_mapping)
    if tokenizer.schema_id != trajectory.schema_id:
        raise ValueError("Model database and snapshot schema differ; supply the matching model database")
    return build_route_bias(model, schema["group_ids"])


def source_hashes():
    paths = [Path(__file__), ROOT / "fabotron/dataset.py", ROOT / "fabotron/loss.py", ROOT / "fabotron/layout.py",
             ROOT / "fabotron/token_view.py", ROOT / "fabotron/data_collection/common.py",
             *sorted((ROOT / "fabotron/model").glob("*.py"))]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--database", type=Path, nargs="+", help="Token V2 SQLite databases (temporal split)")
    sources.add_argument("--collection-manifest", type=Path, help="Complete collection manifest; hold out whole simulation seeds")
    parser.add_argument("--run-id", type=int, help="Only this run_id; default includes every Tokenizer run")
    parser.add_argument("--model-database", type=Path, default=ROOT / "data/model/fab_model.sqlite")
    parser.add_argument("--no-route-bias", action="store_true")
    parser.add_argument("--history-len", type=int, default=8)
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--val-fraction", type=float, default=0.15)
    for name in ("c", "w", "q", "p"):
        parser.add_argument(f"--lambda-{name}", type=float, default=1.0)
    parser.add_argument("--d-model", type=int, default=64)
    parser.add_argument("--ff-mult", type=int, default=4)
    parser.add_argument("--c-hidden", type=int, help="C head hidden width; default = d_model")
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--patience", type=int, default=10)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--weight-decay", type=float, default=0.0001)
    parser.add_argument("--grad-clip", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, cuda:0, ...")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-snapshots", type=int, help="Prefix per run, for short smoke checks")
    args = parser.parse_args(argv)
    if args.collection_manifest and (args.run_id is not None or args.train_fraction != 0.7 or args.val_fraction != 0.15):
        parser.error("Collection manifests define runs and whole-seed splits; do not override them with temporal split options")
    if min(args.history_len, args.batch_size, args.epochs, args.patience, args.threads, args.ff_mult) < 1:
        parser.error("Lengths, batch size, epochs, patience, threads and ff-mult must be positive")
    if args.d_model < 2 or args.d_model % 2:
        parser.error("d-model must be a positive even number for RoPE")
    if not 0 <= args.dropout < 1 or args.lr <= 0 or args.weight_decay < 0 or args.grad_clip < 0:
        parser.error("Invalid dropout, learning rate, weight decay or gradient clip")
    return args


def main(argv=None):
    args = parse_args(argv)
    started = time.perf_counter()
    output_dir = args.output_dir or ROOT / "data/training/fabotron" / datetime.now(HK_TIME).strftime("%Y%m%d_%H%M%S_%f")
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"Output directory is not empty; refusing to overwrite: {output_dir}")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")
    torch.set_num_threads(args.threads)
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True)
    # 确定性模式默认会把每块新分配的显存先填 NaN（每步约 9 ms）；结果不依赖未初始化内存，关掉不影响结果
    torch.utils.deterministic.fill_uninitialized_memory = False
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    print(f"Training device: {device}", flush=True)
    if args.collection_manifest:
        trajectories, datasets = load_collection_datasets(
            args.collection_manifest, args.history_len, args.max_snapshots,
        )
        databases = [trajectory.database for trajectory in trajectories]
    else:
        databases = args.database
        trajectories = load_trajectories(databases, run_id=args.run_id, max_snapshots=args.max_snapshots)
        datasets = make_datasets(trajectories, args.history_len, args.train_fraction, args.val_fraction)
    schema = trajectories[0].schema
    objective = FabotronLoss({"C": args.lambda_c, "W": args.lambda_w, "Q": args.lambda_q, "P": args.lambda_p})
    generator = torch.Generator().manual_seed(args.seed)
    loaders = {
        name: DataLoader(dataset, batch_size=args.batch_size, shuffle=name == "train", num_workers=0,
                         pin_memory=device.type == "cuda", generator=generator if name == "train" else None)
        for name, dataset in datasets.items()
    }
    print("Sequences: " + ", ".join(f"{name}={len(dataset)}" for name, dataset in datasets.items()), flush=True)
    bias = None if args.no_route_bias else build_bias(args.model_database, trajectories[0])
    model_config = {"d_model": args.d_model, "ff_mult": args.ff_mult, "dropout": args.dropout, "c_hidden": args.c_hidden}
    # 只用训练集拟合 σ、区间代表值、C 的分档和先验，再按分档建模型。
    # 末层置零 + 偏置 = 先验，初始输出就是边际分布。
    coder = TargetCoder(TokenLayout(schema))
    calibration = coder.calibrate(datasets["train"])
    print("C tiers (column -> {R: groups}): " + json.dumps(calibration["C"]["groups_per_tier"]), flush=True)
    model = Fabotron(schema, coder, **model_config, route_bias=bias)
    model.init_from_coder()
    model = model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
    config["database"] = [str(Path(path).resolve()) for path in databases]
    config["output_dir"] = str(output_dir)
    config["split_method"] = "whole_simulation_seed" if args.collection_manifest else "within_trajectory_time"
    if args.collection_manifest:
        config["collection_manifest"] = str(args.collection_manifest.resolve())
        config["collection_manifest_sha256"] = hashlib.sha256(args.collection_manifest.read_bytes()).hexdigest()
    manifest = {
        "started_at": datetime.now(HK_TIME).isoformat(), "config": config, "device": str(device),
        "torch_version": str(torch.__version__), "model_config": model_config,
        "parameters": sum(p.numel() for p in model.parameters()), "schema_id": trajectories[0].schema_id,
        "schema": schema, "loss": objective.configuration(), "calibration": calibration,
        "trajectories": [trajectory.manifest() for trajectory in trajectories],
        "splits": {name: dataset.segments for name, dataset in datasets.items()},
        "source_sha256": source_hashes(),
    }
    write_json(output_dir / "config.json", manifest)
    baseline_validation = evaluate(model, loaders["validation"], objective, device, trajectories, persistence=True)
    initial_validation = evaluate(model, loaders["validation"], objective, device, trajectories)
    best_loss, best_epoch = initial_validation["loss"], 0
    checkpoint_path = output_dir / "best.pt"

    def save_checkpoint(epoch, validation):
        torch.save({
            "epoch": epoch, "model_state": {key: value.detach().cpu() for key, value in model.state_dict().items()},
            "optimizer_state": optimizer.state_dict(), "model_config": model_config,
            "schema_id": trajectories[0].schema_id, "schema": schema, "loss_config": objective.configuration(),
            "history_len": args.history_len, "sample_interval_minutes": trajectories[0].interval_minutes,
            "validation": validation, "source_sha256": manifest["source_sha256"], "config": config,
        }, checkpoint_path)

    save_checkpoint(0, initial_validation)
    history = [{"epoch": 0, "validation": initial_validation}]
    print(f"Initial validation={best_loss:.6f} (marginal prior); persistence WIP MAE="
          f"{baseline_validation['wip_mae_lots']:.3f}", flush=True)
    epochs_run = 0
    for epoch in range(1, args.epochs + 1):
        loss_sum = 0.0
        samples = 0
        epoch_started = time.perf_counter()
        for batch in loaders["train"]:
            tables, _ = to_device(batch, device)
            loss = train_one_batch(model, tables, optimizer, objective, args.grad_clip)
            loss_sum += loss.double() * len(tables[0])
            samples += len(tables[0])
        full_eval = epoch % EVAL_INTERVAL == 0
        validation = (evaluate(model, loaders["validation"], objective, device, trajectories) if full_eval
                      else evaluate_loss(model, loaders["validation"], objective, device))
        row = {"epoch": epoch, "train_loss": float(loss_sum / samples), "validation": validation,
               "seconds": time.perf_counter() - epoch_started}
        history.append(row)
        epochs_run = epoch
        if validation["loss"] < best_loss:
            best_loss, best_epoch = validation["loss"], epoch
            save_checkpoint(epoch, validation)
        write_json(output_dir / "history.json", history)
        metric = f" WIP MAE={validation['wip_mae_lots']:.3f}" if full_eval else ""
        print(f"Epoch {epoch:03d}: train={row['train_loss']:.6f} validation={validation['loss']:.6f}"
              f"{metric} best_epoch={best_epoch} ({row['seconds']:.1f}s)", flush=True)
        if epoch - best_epoch >= args.patience:
            print("Early stopping", flush=True)
            break
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    model.load_state_dict(checkpoint["model_state"])
    selected_validation = evaluate(model, loaders["validation"], objective, device, trajectories)
    selected_test = evaluate(model, loaders["test"], objective, device, trajectories)
    baseline_test = evaluate(model, loaders["test"], objective, device, trajectories, persistence=True)
    summary = {
        "completed_at": datetime.now(HK_TIME).isoformat(), "best_epoch": best_epoch, "epochs_run": epochs_run,
        "wall_seconds": time.perf_counter() - started, "checkpoint": str(checkpoint_path),
        "input_shape": {"step_counts": ["batch", args.history_len, *schema["step_counts"]["shape"]],
                        "group_features": ["batch", args.history_len, *schema["group_features"]["shape"]]},
        "output": "categorical: C change per column, ΔW/ΔQ sigma bins, per-group cell composition",
        "loss": objective.configuration(), "initial_validation": initial_validation,
        "selected_validation": selected_validation, "selected_test": selected_test,
        "persistence_validation": baseline_validation, "persistence_test": baseline_test,
        "limitations": [
            "Point metrics use the median bin's in-bin mean; binning adds about 0.5 lot quantization error to WIP MAE.",
            "Groups are sampled independently given the history: fab-wide lot conservation is not enforced.",
            "M/B are not predicted; policy is an input. Persistence has no likelihood, only point metrics.",
            ("Simulation seeds are held out by the collection manifest; no rollout evaluation is performed."
             if args.collection_manifest else
             "Time holdout within a trajectory is not independent simulation-seed evaluation; no rollout evaluation is performed."),
        ],
    }
    write_json(output_dir / "summary.json", summary)
    print(f"Selected test loss={selected_test['loss']:.6f}; WIP MAE {selected_test['wip_mae_lots']:.3f} "
          f"vs persistence {baseline_test['wip_mae_lots']:.3f}", flush=True)
    print(f"Saved training artifacts: {output_dir}", flush=True)
    return summary


if __name__ == "__main__":
    main()
