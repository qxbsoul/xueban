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


class QuestionPerformanceTest(unittest.TestCase):
    """任务书第二节：应作答/实际/有效/复核/失败/未判/未作答，修订与跨节点不重复计数。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_question_perf_worker.py")

    def test_partition_and_reconciliation(self):
        summary = self.payload["summary"]
        self.assertTrue(summary["partition_ok"], summary)
        # 只有题目池、没有逐生题单：应作答/未作答必须为未知，不得用题目池×人数推算。
        self.assertFalse(summary["expected_known"], summary)
        self.assertIsNone(summary["expected"], summary)
        self.assertIsNone(summary["expected_ok"], summary)
        self.assertEqual(summary["pool_reference"], 12, summary)
        self.assertEqual(summary["unknown_scope_questions"], 3, summary)
        self.assertEqual(summary["answered"], summary["graded"] + summary["review"] + summary["failed"] + summary["pending"], summary)

    def test_q001_counts_and_states(self):
        data = self.payload["Q001"]
        self.assertIsNone(data["expected"], data)
        self.assertEqual(data["pool_reference"], 4, data)
        self.assertEqual(data["answered"], 4, data)
        self.assertIsNone(data["not_answered"], data)
        self.assertEqual(data["expected_basis"], "unknown", data)
        self.assertEqual(data["graded"], 2, data)
        self.assertEqual(data["review"], 1, data)
        self.assertEqual(data["failed"], 1, data)
        self.assertAlmostEqual(data["average_score"], 1.0, places=2, msg=data)

    def test_revision_is_not_double_counted(self):
        # S1 有 r1/r2 两次提交，只能算 1 个实际作答、1 个有效判题，且取最新 r2 的分数。
        data = self.payload["Q001"]
        self.assertEqual(data["answered"], 4, data)
        self.assertEqual(data["graded"], 2, data)
        self.assertAlmostEqual(data["average_score"], 1.0, places=2, msg=data)

    def test_same_student_two_nodes_counts_once_and_blank_is_not_answered(self):
        data = self.payload["Q002"]
        self.assertIsNone(data["expected"], data)
        self.assertEqual(data["pool_reference"], 4, data)
        self.assertEqual(data["answered"], 1, data)
        self.assertIsNone(data["not_answered"], data)
        self.assertEqual(data["unknown_submission"], 1, data)
        self.assertEqual(data["graded"], 1, data)

    def test_out_of_scope_answer_is_excluded_from_expected(self):
        data = self.payload["Q999"]
        self.assertEqual(data["expected"], 0, data)
        self.assertEqual(data["answered"], 1, data)
        self.assertEqual(data["out_of_scope_answered"], 1, data)
        self.assertEqual(data["expected_basis"], "out_of_scope", data)
        self.assertEqual(data["not_answered"], 0, data)

    def test_class_scope_uses_class_participants(self):
        summary = self.payload["class_one"]
        self.assertEqual(summary["questions"], 4, summary)
        self.assertIsNone(summary["expected"], summary)
        self.assertEqual(summary["pool_reference"], 9, summary)

    def test_detail_includes_not_answered_students(self):
        detail = self.payload["detail"]
        self.assertIsNone(detail["partition"]["expected"], detail)
        self.assertEqual(detail["partition"]["answered"], 1, detail)
        self.assertEqual(detail["students"], 2, detail)
        self.assertIn("pending", detail["statuses"], detail)
        self.assertNotIn("not_answered", detail["statuses"], detail)
        self.assertEqual(detail["scope"]["basis"], "unknown", detail)
        self.assertIn("confirmed_unsubmitted", detail["lists"], detail)


if __name__ == "__main__":
    unittest.main()
