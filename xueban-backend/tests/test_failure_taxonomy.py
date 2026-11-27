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
        env["XUEBAN_GRADER_ATTEMPTS"] = "2"
        env["XUEBAN_GRADER_RETRY_BASE_MS"] = "0"
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class FailureTaxonomyTest(unittest.TestCase):
    """v1.5.17：判题失败分类、结构修复重试、幂等与重试人工保护。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_failure_taxonomy_worker.py")

    def test_normal_grading(self):
        data = self.payload["ok"]
        self.assertEqual(data["status"], "graded", data)
        self.assertEqual(data["calls"], 1, data)

    def test_awarded_over_max_is_capped_to_review(self):
        # v1.5.21：单点评分不可能超过原始满分；确定性截断后进入人工复核，不伪装成功、也不永久失败。
        data = self.payload["awarded_over_max"]
        self.assertEqual(data["status"], "needs_review", data)
        self.assertTrue(data["review"], data)
        self.assertEqual(data["score"], 2, data)

    def test_declared_max_above_original_is_repaired_not_failed(self):
        data = self.payload["declared_max_clamped"]
        self.assertIn(data["status"], ("graded", "needs_review"), data)
        self.assertEqual(data["score"], 2, data)
        self.assertIn("scoring_point_declared_max_clamped", data["issues"], data)

    def test_unknown_scoring_point_is_mapping_error_without_retry(self):
        data = self.payload["unknown_point"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "scoring_point_mapping_error", data)
        self.assertEqual(data["calls"], 1, data)

    def test_null_step_index_is_score_type_error_not_crash(self):
        data = self.payload["null_step_index"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "model_incomplete_output", data)
        self.assertEqual(data["exception_class"], "GradeFailure", data)

    def test_null_step_score_is_score_type_error(self):
        data = self.payload["null_step_score"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "score_type_error", data)
        self.assertEqual(data["calls"], 2, data)

    def test_empty_answer_is_not_sent_to_model(self):
        data = self.payload["empty_answer"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "answer_missing", data)
        self.assertEqual(data["calls"], 0, data)

    def test_db_persist_error_is_classified(self):
        data = self.payload["db_persist"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "db_persist_error", data)

    def test_structure_error_retries_once_then_succeeds(self):
        data = self.payload["structure_retry_success"]
        self.assertIn(data["status"], ("graded", "needs_review"), data)
        self.assertEqual(data["calls"], 2, data)

    def test_timeout_is_retried_and_classified(self):
        data = self.payload["timeout"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["error_type"], "network_timeout", data)
        self.assertEqual(data["attempts"], 2, data)
        self.assertTrue(data["retryable"], data)

    def test_completed_answer_is_not_graded_twice(self):
        data = self.payload["idempotent"]
        self.assertTrue(data["reused"], data)
        self.assertEqual(data["runs_before"], data["runs_after"], data)
        self.assertEqual(data["evidence_before"], data["evidence_after"], data)

    def test_retry_only_failed_and_skips_manual(self):
        data = self.payload["retry"]
        self.assertEqual(data["retried"], 2, data)
        self.assertEqual(data["skipped_manual"], 1, data)
        self.assertEqual(data["statuses"]["ri_1"], "failed", data)
        self.assertEqual(data["statuses"]["ri_2"], "pending", data)
        self.assertEqual(data["statuses"]["ri_3"], "pending", data)
        self.assertEqual(data["manual_grade_status"], "manual_corrected", data)


if __name__ == "__main__":
    unittest.main()
