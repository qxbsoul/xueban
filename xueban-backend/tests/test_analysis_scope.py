import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_analysis_scope_worker.py")


class AnalysisScopeTest(unittest.TestCase):
    def test_three_class_compare_and_longitudinal(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["class_count"], 3)
            self.assertEqual(payload["row_values"][0], 3)
            self.assertEqual(payload["longitudinal_points"], 2)
            self.assertEqual(payload["longitudinal_status"], "unverifiable")

    def test_versioned_trend_is_comparable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER), "versioned"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["trend_status"], "comparable")


if __name__ == "__main__":
    unittest.main()
