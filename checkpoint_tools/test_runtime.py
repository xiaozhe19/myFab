from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from .__main__ import new_engine
from .runtime import (DAY, ROOT, MetricsObserver, advance_to, attach_observer,
                      fingerprint, load_checkpoint, save_checkpoint, source_hashes, write_json)


class CheckpointIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parent = ROOT / "data/checkpoints/selftests"
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.resolve().is_relative_to(ROOT):
            raise ValueError("Test directory outside workspace.")
        cls.temp = tempfile.TemporaryDirectory(prefix="checkpoint_", dir=parent)
        cls.directory = Path(cls.temp.name)
        assert cls.directory.resolve().is_relative_to(parent.resolve())
        cls.observer = MetricsObserver()
        cls.engine = new_engine(ROOT / "data/model/fab_model.sqlite", 2026061700, cls.observer)
        advance_to(cls.engine, DAY)
        cls.path = cls.directory / "state.fabcp"
        cls.manifest = save_checkpoint(cls.path, cls.engine, seed=2026061700, requested_day=1,
                                       model_database=ROOT / "data/model/fab_model.sqlite",
                                       expected_sources=source_hashes())

    @classmethod
    def tearDownClass(cls):
        assert cls.directory.resolve().is_relative_to(ROOT / "data/checkpoints/selftests")
        cls.temp.cleanup()

    def modified_copy(self, name, transform_manifest=lambda m: m, transform_payload=lambda b: b):
        target = self.directory / name
        with zipfile.ZipFile(self.path) as old, zipfile.ZipFile(target, "x") as new:
            manifest = transform_manifest(json.loads(old.read("manifest.json")))
            new.writestr("manifest.json", json.dumps(manifest))
            new.writestr("engine.pkl", transform_payload(old.read("engine.pkl")))
        return target

    def test_01_fresh_process_continuation_matches_uninterrupted(self):
        restored, _ = load_checkpoint(self.path)
        self.assertIs(restored._downtime.engine, restored)
        self.assertEqual(fingerprint(restored), self.manifest["fingerprint"])
        self.assertGreater(len(restored.lots), 0)
        observer = attach_observer(restored)
        self.assertEqual(len(observer.wip), len(restored.lots))
        self.observer.start_probe()
        advance_to(self.engine, self.engine.current_time + DAY)
        expected = {"start_fingerprint": self.manifest["fingerprint"],
                    "end_fingerprint": fingerprint(self.engine),
                    "until_minute": self.engine.current_time,
                    "trace": self.observer.finish_probe()}
        expected_path = self.directory / "expected.json"
        result_path = self.directory / "verified.json"
        write_json(expected_path, expected)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "checkpoint_tools", "verify", "--checkpoint", str(self.path),
             "--expected", str(expected_path), "--output", str(result_path)],
            cwd=ROOT, capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        verified = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertTrue(verified["passed"])
        self.assertGreater(verified["trace"]["counts"]["operation"], 0)

    def test_02_corrupted_payload_is_rejected(self):
        corrupt = self.modified_copy("corrupt.fabcp", transform_payload=lambda b: b[:-1] + bytes([b[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "integrity"):
            load_checkpoint(corrupt)

    def test_03_wrong_runtime_is_rejected_before_loading(self):
        def change(manifest):
            manifest["python"] = [0, 0, 0]
            return manifest
        wrong = self.modified_copy("runtime.fabcp", transform_manifest=change)
        with self.assertRaisesRegex(ValueError, "runtime"):
            load_checkpoint(wrong)

    def test_04_checkpoint_files_are_not_overwritten(self):
        with self.assertRaises(FileExistsError):
            save_checkpoint(self.path, self.engine, seed=2026061700, requested_day=2,
                            model_database=ROOT / "data/model/fab_model.sqlite",
                            expected_sources=source_hashes())


if __name__ == "__main__":
    unittest.main()
