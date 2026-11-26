import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_cross_batch_worker.py")


class CrossBatchIsolationTest(unittest.TestCase):
    def test_two_batches_keep_independent_question_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "isolated.sqlite")
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = db_path
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["answers"], 2)
            self.assertEqual(payload["a_max"], 5.0)
            self.assertEqual(payload["b_max"], 10.0)
            self.assertNotEqual(payload["a_stem"], payload["b_stem"])
            self.assertAlmostEqual(payload["a_mastery"], 0.4, places=4)
            self.assertAlmostEqual(payload["b_mastery"], 0.5, places=4)
            self.assertLess(payload["a_mastery"], payload["b_mastery"])
            self.assertEqual(payload["a_batch_answers"], 1)
            self.assertEqual(payload["b_batch_answers"], 1)

    def test_deleting_one_batch_keeps_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER), "delete"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["b_answers"], 1)
            self.assertEqual(payload["b_versions"], 1)
            self.assertEqual(payload["a_batches"], 0)


if __name__ == "__main__":
    unittest.main()
