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
    env["XUEBAN_COMPARE_MIN_SAMPLE"] = "3"
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class ClassAnalyticsTest(unittest.TestCase):
    """任务书第四/五/六项：分布对账、比较差值、样本不足、跨任务可比性。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_class_analytics_worker.py")

    def test_status_breakdown_covers_every_student(self):
        data = self.payload["class_a"]
        self.assertEqual(data["sum_check"], True, data)
        self.assertEqual(data["breakdown_sum"], data["students"], data)
        self.assertEqual(data["students_in_breakdown"], data["students"], data)
        self.assertEqual(data["status_counts"].get("证据不足"), 1, data)

    def test_mean_excludes_insufficient_students(self):
        data = self.payload["class_a"]
        self.assertEqual(data["valid_students"], data["students"] - 1, data)
        self.assertIsNotNone(data["mean"], data)

    def test_compare_uses_pp_and_sorted_by_difference(self):
        data = self.payload["compare"]
        self.assertTrue(data["sorted_desc"], data)
        row = data["first"]
        if row["difference"] is not None:
            self.assertAlmostEqual(row["pp_difference"], round(row["difference"] * 100, 2), places=2, msg=data)
        self.assertEqual(data["thresholds"]["min_sample"], 3, data)

    def test_compare_reports_insufficient_samples(self):
        data = self.payload["compare"]
        self.assertTrue(data["insufficient"] or data["advantages"] or data["common_weak"], data)

    def test_knowledge_detail_links_questions_and_evidence(self):
        data = self.payload["kp_detail"]
        self.assertGreaterEqual(data["questions"], 1, data)
        self.assertGreaterEqual(data["scoring_points"], 1, data)
        self.assertGreaterEqual(data["evidence"], 3, data)

    def test_multi_task_excludes_incompatible_batch(self):
        data = self.payload["student_multi"]
        self.assertEqual(data["included"], ["batch_cb1"], data)
        self.assertTrue(data["excluded"], data)
        self.assertIn("满分不同", data["reason"], data)
        self.assertGreater(data["single_evidence"], 0, data)


if __name__ == "__main__":
    unittest.main()
