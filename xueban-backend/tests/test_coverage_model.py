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


class CoverageModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_coverage_worker.py")

    def test_full_answer_reaches_full_coverage_even_with_many_questions(self):
        student = self.payload["SA"]
        self.assertAlmostEqual(student["IB"]["data_coverage"], 1.0, places=4)
        self.assertEqual(student["IB"]["covered_question_count"], 10)
        self.assertEqual(student["IB"]["opportunity_question_count"], 10)
        self.assertNotEqual(student["IB"]["status"], "证据不足", student["IB"])
        self.assertAlmostEqual(student["IA"]["data_coverage"], 1.0, places=4)
        self.assertNotEqual(student["IA"]["status"], "证据不足", student["IA"])

    def test_partial_single_question_is_insufficient(self):
        student = self.payload["SB"]
        self.assertAlmostEqual(student["IA"]["data_coverage"], 0.5, places=4)
        self.assertEqual(student["IA"]["covered_question_count"], 1)
        self.assertEqual(student["IA"]["status"], "证据不足", student["IA"])

    def test_coverage_denominator_is_documented(self):
        self.assertEqual(self.payload["parameters"]["coverage_denominator"], "该知识点在任务中的可作答题目数")

    def test_beta_quantile_is_exact(self):
        checks = self.payload["beta_check"]
        self.assertAlmostEqual(checks["0.025"], 0.025, places=5)
        self.assertAlmostEqual(checks["0.5"], 0.5, places=5)
        self.assertAlmostEqual(checks["0.975"], 0.975, places=5)


if __name__ == "__main__":
    unittest.main()
