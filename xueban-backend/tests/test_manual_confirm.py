import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_manual_confirm_worker.py")


class ManualConfirmTest(unittest.TestCase):
    def test_point_confirmation_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["matches"])
            self.assertEqual(payload["status"], "manual_confirmed")
            self.assertEqual(payload["confirmations"], 1)
            self.assertGreater(payload["mastery"], 0.5)

    def test_mismatched_point_total_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER), "mismatch"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertFalse(payload["matches"])
            self.assertEqual(payload["status"], "needs_review")

    def test_rejudge_keeps_manual_confirmation_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER), "rejudge"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["confirmations"], 1)
            self.assertEqual(payload["latest_version"], 2)


if __name__ == "__main__":
    unittest.main()
