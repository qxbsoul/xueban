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


class DataQualityModelTest(unittest.TestCase):
    """任务书第三节 + 第十四节 7–12：主状态互斥、总数对账、题目级异常可见、缺失三态。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_data_quality_v2_worker.py")

    def test_total_reconciles(self):
        summary = self.payload["summary"]
        self.assertTrue(summary["reconciled"], summary)
        self.assertEqual(summary["valid"] + summary["abnormal"] + summary["pending_review"] + summary["excluded"], summary["total"], summary)

    def test_question_level_issue_marks_all_its_answers(self):
        data = self.payload["questions"]["Q001"]
        self.assertEqual(data["total"], 2, data)
        self.assertGreaterEqual(data["abnormal"], 1, data)
        self.assertIn("scoring_points_sum_mismatch", data["issues"], data)
        records = self.payload["records"]
        self.assertEqual(records["a_q1_ok"]["status"], "abnormal", records["a_q1_ok"])

    def test_illegal_scoring_point_max_is_detected(self):
        types = dict(self.payload["report"]["by_type"])
        self.assertGreaterEqual(types.get("scoring_point_max_invalid", 0), 1, types)
        self.assertIn("scoring_point_max_invalid", self.payload["questions"]["Q003"]["issues"], self.payload["questions"]["Q003"])

    def test_no_issue_is_silently_dropped(self):
        self.assertEqual(self.payload["unlinked"], [], self.payload["unlinked"])
        self.assertEqual(self.payload["issues_linked"], self.payload["issues_total"], self.payload)

    def test_three_kinds_of_missing_answer_are_distinguished(self):
        types = dict(self.payload["report"]["by_type"])
        self.assertEqual(types.get("explicit_unsubmitted", 0), 1, types)
        self.assertEqual(types.get("empty_answer_unknown", 0), 1, types)
        missing = self.payload["report"]["missing"]
        self.assertEqual(missing["explicit_unsubmitted"], 1, missing)
        self.assertEqual(missing["unknown"], 1, missing)
        records = self.payload["records"]
        self.assertEqual(records["a_q2_unsubmitted"]["status"], "valid", records["a_q2_unsubmitted"])
        # 该作答同时有空答案告警与题目级阻断问题：主状态取优先级最高的 abnormal，但告警仍保留可追溯。
        self.assertEqual(records["a_q1_empty"]["status"], "abnormal", records["a_q1_empty"])
        self.assertIn("empty_answer_unknown", records["a_q1_empty"]["issues"], records["a_q1_empty"])

    def test_blocking_list_is_exposed(self):
        self.assertIn("scoring_points_sum_mismatch", self.payload["blocking"], self.payload["blocking"])


if __name__ == "__main__":
    unittest.main()
