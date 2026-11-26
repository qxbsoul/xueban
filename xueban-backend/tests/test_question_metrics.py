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


class QuestionMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_metrics_worker.py")

    def test_q011_student_equal_weight(self):
        q = self.payload["Q011"]
        self.assertAlmostEqual(q["average_score"], 1.0)
        self.assertAlmostEqual(q["student_average_score"], 1.33, places=2)
        self.assertEqual(q["student_score_unit_count"], 3)
        self.assertEqual(q["valid_score_students"], 3)
        sp = {item[0]: item for item in self.payload["Q011_sp"]}
        self.assertAlmostEqual(sp["Q011P1"][1], 0.67, places=2)
        self.assertAlmostEqual(sp["Q011P1"][2], 0.5, places=2)

    def test_q014_student_equal_weight(self):
        q = self.payload["Q014"]
        self.assertAlmostEqual(q["average_score"], 1.0)
        self.assertAlmostEqual(q["student_average_score"], 0.8, places=2)
        self.assertEqual(q["student_score_unit_count"], 5)

    def test_q015_student_equal_weight(self):
        q = self.payload["Q015"]
        self.assertAlmostEqual(q["average_score"], 2.0)
        self.assertAlmostEqual(q["student_average_score"], 2.0, places=2)
        self.assertEqual(q["student_score_unit_count"], 2)

    def test_manual_correction_counts_score_not_scoring_point_evidence(self):
        q = self.payload["Q016"]
        self.assertAlmostEqual(q["average_score"], 3.0)
        self.assertAlmostEqual(q["student_average_score"], 3.0)
        self.assertEqual(q["manual_correction_without_scoring_sync"], 1)
        # 人工修正的学生不得出现在评分点统计里
        for sp in self.payload["Q016_sp"]:
            self.assertEqual(sp[1], 1, self.payload["Q016_sp"])
        statuses = {item[0]: item[1] for item in self.payload["Q016_students"]}
        self.assertEqual(statuses["S02"], "manual_corrected")
        self.assertEqual(statuses["S01"], "graded")

    def test_manual_correction_with_point_confirmation_produces_confirmed_evidence(self):
        evidence = self.payload["Q017_evidence"]
        self.assertEqual(len(evidence), 1, evidence)
        self.assertEqual(evidence[0]["sp"], "Q017P1")
        self.assertEqual(evidence[0]["earned"], 2.0)
        self.assertEqual(evidence[0]["source"], "teacher_confirmed")
        self.assertGreaterEqual(self.payload["Q017_quality"].get("manual_corrected_without_point_confirmation", 0), 1)


if __name__ == "__main__":
    unittest.main()
