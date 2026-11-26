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


class MethodCoverageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_method_worker.py")

    def test_coverage_uses_independent_opportunity_denominator(self):
        m1 = self.payload["M1"]
        self.assertEqual(m1["opportunity_question_count"], 3, m1)
        self.assertEqual(m1["covered_question_count"], 2, m1)
        self.assertAlmostEqual(m1["data_coverage"], 0.6667, places=3)
        self.assertNotEqual(m1["status"], "暂不可计算", m1)

    def test_opportunity_without_evidence_is_not_100_percent(self):
        m2 = self.payload["M2"]
        self.assertEqual(m2["opportunity_question_count"], 1, m2)
        self.assertEqual(m2["covered_question_count"], 0, m2)
        self.assertAlmostEqual(m2["data_coverage"], 0.0, places=4)
        self.assertEqual(m2["status"], "暂无证据", m2)

    def test_missing_opportunity_returns_null_not_100_percent(self):
        m3 = self.payload["M3"]
        self.assertIsNone(m3["data_coverage"], m3)
        self.assertEqual(m3["opportunity_question_count"], 0, m3)
        self.assertEqual(m3["status"], "暂不可计算", m3)
        self.assertEqual(m3["coverage_status"], "机会数据缺失", m3)

    def test_single_question_evidence_is_concentrated_and_weak(self):
        m4 = self.payload["M4"]
        self.assertEqual(m4["covered_question_count"], 1, m4)
        self.assertTrue(m4["evidence_distribution"]["concentrated_on_single_question"], m4)
        self.assertEqual(m4["status"], "证据不足", m4)


if __name__ == "__main__":
    unittest.main()
