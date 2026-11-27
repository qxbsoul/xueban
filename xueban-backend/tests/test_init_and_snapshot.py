import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_worker(name: str):
    env = dict(os.environ)
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class InitAndSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_perf_worker.py")

    def test_init_is_cheap_after_bootstrap(self):
        self.assertLess(self.payload["init_20_seconds"], 0.2, self.payload)
        self.assertEqual(self.payload["init_write_statements"], 0, self.payload)

    def test_migrations_recorded_once(self):
        self.assertEqual(self.payload["migrations"], [1, 2, 3, 4, 5, 6], self.payload)

    def test_identical_snapshot_is_reused(self):
        self.assertTrue(self.payload["snapshot_same_id"], self.payload)
        self.assertEqual(self.payload["snapshot_count_after_duplicate"], 1, self.payload)

    def test_snapshot_retention_is_bounded(self):
        self.assertLessEqual(self.payload["snapshot_final_count"], self.payload["snapshot_max_retained"], self.payload)
        self.assertTrue(self.payload["snapshot_latest_payload_hash_present"], self.payload)


if __name__ == "__main__":
    unittest.main()
