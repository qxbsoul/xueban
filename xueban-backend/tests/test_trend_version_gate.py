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


class TrendVersionGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_trend_v2_worker.py")

    def test_different_versions_are_non_comparable(self):
        self.assertEqual(self.payload["compat_required"]["status"], "non_comparable", self.payload)
        self.assertIn("grading_rule_version", self.payload["compat_required"]["reason"])
        self.assertEqual(self.payload["diff"][1]["status"], "non_comparable", self.payload["diff"])

    def test_explicit_compatibility_allows_comparable(self):
        self.assertEqual(self.payload["compat_after"]["status"], "comparable", self.payload["compat_after"])

    def test_same_versions_are_comparable(self):
        self.assertEqual(self.payload["same"][1]["status"], "comparable", self.payload["same"])

    def test_student_group_change_warns_even_when_comparable(self):
        overlap = self.payload["overlap"]
        self.assertEqual(overlap["status"], "comparable", overlap)
        self.assertAlmostEqual(overlap["rate"], 0.5, places=4)
        self.assertIn("重合率", overlap["warning"])
        self.assertEqual(overlap["only_right"], ["student_extra"], overlap)


if __name__ == "__main__":
    unittest.main()
