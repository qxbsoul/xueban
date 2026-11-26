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
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class TrendComparabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_trend_worker.py")

    def test_structural_difference_is_non_comparable_even_with_versions(self):
        points = self.payload["struct"]
        self.assertEqual(points[0]["status"], "unverifiable")
        self.assertEqual(points[1]["status"], "non_comparable", points)
        self.assertIn("题目集合不同", points[1]["reason"])
        self.assertEqual(points[1]["diff"]["questions_only_left"], ["Q2"], points)

    def test_same_structure_and_versions_is_comparable(self):
        points = self.payload["ok"]
        self.assertEqual(points[1]["status"], "comparable", points)
        self.assertEqual(points[1]["ewma"], 100.0, points)

    def test_missing_versions_is_unverifiable_with_reason(self):
        points = self.payload["unver"]
        self.assertEqual(points[1]["status"], "unverifiable", points)
        self.assertIn("缺少", points[1]["reason"])

    def test_ewma_keeps_gaps_and_smooths_values(self):
        self.assertEqual(self.payload["ewma_none"], [50.0, None, 60.0])
        self.assertEqual(self.payload["ewma_basic"], [10.0, 15.0, 22.5])


if __name__ == "__main__":
    unittest.main()
