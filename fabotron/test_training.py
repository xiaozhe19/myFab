"""
Run with: python -m unittest fabotron.test_training.
"""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import numpy as np
import torch

from fabotron.dataset import load_trajectories, make_datasets
from fabotron.loss import TokenStateLoss
from fabotron.model.fabotron import Fabotron
from fabotron.train import main, train_one_batch


ROOT = Path(__file__).resolve().parents[1]


def fixture_schema(groups=("A", "B")):
    sizes = [("W", 1), ("P_WIP", 6), ("Q", 1), ("P_Queue", 2),
             ("C", 4), ("M", 2), ("B", 2), ("policy", 2)]
    slices, offset = {}, 0
    for name, width in sizes:
        slices[name] = [offset, offset + width]
        offset += width
    return {"version": "test", "group_ids": list(groups), "input_dim": offset, "slices": slices,
            "normalization": {"W": 10, "Q": 10},
            "C_columns": ["total_tools", "available_tools", "occupied_slots", "down_tools"]}


def token(schema, index=0, run_id=1):
    result = np.zeros((len(schema["group_ids"]), schema["input_dim"]), dtype=np.float32)
    result[:, 0] = 0.3 + index * 0.01 + run_id * 0.1
    result[:, 1:3] = 0.5
    result[:, 7] = result[:, 0] / 2
    result[:, 8] = 1
    result[:, 10:14] = [1, 0.5, 0.25, 0.1]
    result[:, [14, 16, 18]] = 1
    return result


def write_database(path, schema=None, runs=(1, 2)):
    schema = schema or fixture_schema()
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    schema_id = hashlib.sha256(canonical.encode()).hexdigest()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("""CREATE TABLE plugin_records (
            record_id INTEGER PRIMARY KEY, run_id INTEGER, plugin_name TEXT,
            record_type TEXT, event_time REAL, payload_json TEXT)""")
        for run in runs:
            record = {"schema_id": schema_id, "tokenizer_schema": schema, "sample_interval_minutes": 1}
            connection.execute("INSERT INTO plugin_records VALUES(NULL,?,'tokenizer','schema',0,?)",
                               (run, json.dumps(record)))
            for index in range(12):
                snapshot = {"schema_id": schema_id, "group_ids": schema["group_ids"],
                            "time": index, "matrix": token(schema, index, run).tolist()}
                connection.execute("INSERT INTO plugin_records VALUES(NULL,?,'tokenizer','snapshot',?,?)",
                                   (run, index, json.dumps(snapshot)))
        connection.commit()


class TrainingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="fabotron_test_", dir=ROOT / "tmp")
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "fixture.sqlite"
        write_database(self.database)
        self.schema = fixture_schema()
        torch.set_num_threads(1)

    def tearDown(self):
        self.temporary.cleanup()

    def test_full_target_alignment_and_disjoint_splits(self):
        trajectories = load_trajectories([self.database])
        datasets = make_datasets(trajectories, 2, 0.5, 0.25)
        self.assertEqual([len(datasets[k]) for k in ("train", "validation", "test")], [8, 2, 2])
        for dataset in datasets.values():
            for i, (trajectory_id, start) in enumerate(dataset.samples):
                x, y, actual_id = dataset[i]
                self.assertEqual(actual_id, trajectory_id)
                self.assertEqual(tuple(x.shape), (2, 2, 20))
                self.assertEqual(tuple(y.shape), (2, 20))
                torch.testing.assert_close(y, trajectories[trajectory_id].tokens[start + 2])
        for trajectory_id in range(2):
            used = []
            for dataset in datasets.values():
                indices = set()
                for current, start in dataset.samples:
                    if current == trajectory_id:
                        indices.update(range(start, start + 3))
                used.append(indices)
            self.assertFalse(used[0] & used[1] or used[1] & used[2] or used[0] & used[2])

    def test_irregular_times_rejected(self):
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute("SELECT record_id,payload_json FROM plugin_records WHERE record_type='snapshot' AND run_id=1 AND event_time=7").fetchone()
            payload = json.loads(row[1])
            payload["time"] = 7.5
            connection.execute("UPDATE plugin_records SET event_time=7.5,payload_json=? WHERE record_id=?", (json.dumps(payload), row[0]))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "irregular"):
            load_trajectories([self.database])

    def test_schema_mismatch_rejected(self):
        other = self.directory / "other.sqlite"
        write_database(other, fixture_schema(("B", "A")))
        with self.assertRaisesRegex(ValueError, "same Tokenizer schema"):
            load_trajectories([self.database, other])

    def test_controls_and_static_capacity_are_not_loss_targets(self):
        y = torch.tensor(token(self.schema)).unsqueeze(0)
        p = y.clone().requires_grad_()
        for mode in ("v1", "block-mse"):
            objective = TokenStateLoss(self.schema, mode)
            altered = p.clone()
            altered[..., 10] = 900
            altered[..., 14:] = 900
            torch.testing.assert_close(objective(altered, y), objective(p, y))
            grad = torch.autograd.grad(objective(p, y), p, retain_graph=True)[0]
            self.assertEqual(grad[..., 10].abs().sum().item(), 0)
            self.assertEqual(grad[..., 14:].abs().sum().item(), 0)

    def test_quantity_weighting_and_predicted_count_distribution(self):
        y = torch.tensor(token(self.schema)).unsqueeze(0)
        p = y.clone()
        p[..., 0] = 0.6  # True WIP=4, predicted WIP=6 Lot.
        parts = TokenStateLoss(self.schema).components(p, y)
        self.assertAlmostEqual(parts["W"].item(), 4 / 5, places=5)
        # Proportions are unchanged, but the number of Lots is wrong.
        self.assertAlmostEqual(parts["P_WIP"].item(), 2 / 6, places=5)

    def test_calibration_uses_training_only_and_handles_zero_reference(self):
        trajectories = load_trajectories([self.database])
        datasets = make_datasets(trajectories, 2, 0.5, 0.25)
        objective = TokenStateLoss(self.schema)
        objective.calibrate(datasets["train"])
        original = dict(objective.scales)
        for trajectory in trajectories:
            trajectory.tokens[6:] = 999
        objective.calibrate(datasets["train"])
        self.assertEqual(original, objective.scales)
        for trajectory in trajectories:
            trajectory.tokens[:6] = 0
        objective.calibrate(datasets["train"])
        self.assertTrue(all(value == 1 for value in objective.scales.values()))
        p = torch.randn(1, 2, 20, requires_grad=True)
        loss = objective(p, torch.zeros_like(p))
        loss.backward()
        self.assertTrue(torch.isfinite(loss) and torch.isfinite(p.grad).all())

    def test_single_batch_updates_weights(self):
        torch.manual_seed(17)
        model = Fabotron(20, d_model=8, dropout=0)
        optimizer = torch.optim.AdamW(model.parameters(), lr=0.001)
        x = torch.randn(2, 2, 2, 20)
        y = torch.tensor(np.stack([token(self.schema), token(self.schema, 1)]))
        initial = model.output_head.weight.detach().clone()
        value = train_one_batch(model, x, y, optimizer, TokenStateLoss(self.schema))
        self.assertTrue(np.isfinite(value))
        self.assertFalse(torch.equal(initial, model.output_head.weight))

    def test_cli_checkpoint_round_trip(self):
        output = self.directory / "output"
        summary = main(["--database", str(self.database), "--no-route-bias", "--history-len", "2",
                        "--train-fraction", "0.5", "--val-fraction", "0.25", "--d-model", "8",
                        "--batch-size", "2", "--epochs", "2", "--threads", "1",
                        "--device", "cpu", "--output-dir", str(output)])
        self.assertEqual(summary["output_shape"], ["batch", 2, 20])
        self.assertTrue(np.isfinite(summary["selected_test"]["loss"]))
        saved = torch.load(output / "best.pt", map_location="cpu", weights_only=True)
        restored = Fabotron(**saved["model_config"], route_bias=saved["model_state"].get("route_bias"))
        restored.load_state_dict(saved["model_state"])
        self.assertEqual(tuple(restored(torch.randn(1, 2, 2, 20)).shape), (1, 2, 20))
        self.assertEqual(saved["loss_config"]["conservation_weight"], 0)
        for name in ("config.json", "history.json", "summary.json"):
            self.assertTrue((output / name).is_file())


if __name__ == "__main__":
    unittest.main()
