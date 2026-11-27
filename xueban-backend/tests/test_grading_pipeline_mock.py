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
    env["DEEPSEEK_API_KEY"] = "test-key-not-real"
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class GradingPipelineMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_grading_mock_worker.py")

    def test_success_path_writes_grade_and_attempt(self):
        ok = self.payload["ok"]
        self.assertEqual(ok["job_status"], "completed", ok)
        self.assertEqual(ok["processed"], 3, ok)
        self.assertEqual(ok["completed"], 3, ok)
        self.assertEqual(ok["items"], {"pending": 0, "running": 0, "completed": 3, "failed": 0}, ok)
        self.assertEqual(ok["grades"], 3)
        self.assertEqual(ok["attempts"], 3)
        self.assertEqual(ok["grade_versions"], [1])
        self.assertEqual(ok["prompt_version"], "teacher-grade-v5")
        self.assertEqual(ok["grader_version"], "v1.5.13")
        fields = ok["attempt_fields"][0]
        self.assertTrue(fields["batch_id"])
        self.assertTrue(fields["student_id"])
        self.assertGreaterEqual(fields["duration_ms"], 0)
        self.assertEqual(fields["retryable"], 0)
        self.assertEqual(fields["grading_version"], 1)
        self.assertEqual(fields["attempt_number"], 1)

    def test_structure_and_request_context_are_persisted(self):
        ok = self.payload["ok"]
        structure = ok["structure"]
        self.assertEqual(len(structure["source_steps"]), 2, structure)
        self.assertIn("validation", structure, structure)
        self.assertEqual(structure["validation"]["reasoning_step_count"], 1, structure)
        self.assertEqual(structure["validation"]["operation_unit_count"], 1, structure)
        self.assertFalse(structure["validation"]["review_required"], structure)
        context = ok["request_context"]
        for key in ("question_version_id", "question_source", "prompt_version", "grader_version", "mapping_version", "grading_model", "source_step_count"):
            self.assertIn(key, context, context)
        self.assertEqual(context["prompt_version"], "teacher-grade-v5", context)

    def test_regrade_creates_new_version_and_keeps_history(self):
        regrade = self.payload["regrade"]
        self.assertEqual(regrade["count"], 2, regrade)
        self.assertEqual(regrade["versions"], [1, 2], regrade)

    def test_failure_path_records_reason_and_retryable_attempt(self):
        failure = self.payload["failure"]
        self.assertIn(failure["job_status"], ("failed", "partially_failed"), failure)
        self.assertGreaterEqual(failure["failed"], 1, failure)
        self.assertIn("failed", failure["item_statuses"], failure)
        self.assertTrue(set(failure["attempt_stages"]).issubset({"structure_validation", "model_response", "json_parse"}), failure)
        self.assertIn(1, failure["attempt_retryable"], failure)
        self.assertIn("failed", failure["grade_status"], failure)

    def test_retry_reuses_job_id_and_completes(self):
        retry = self.payload["retry"]
        self.assertTrue(retry["same_job"])
        self.assertEqual(retry["status"], "completed", retry)
        self.assertEqual(retry["completed"], 3, retry)
        self.assertGreater(retry["attempts"], 3, "重试后 attempt 必须累加")

    def test_pause_does_not_consume_pending_and_resume_finishes(self):
        pause = self.payload["pause"]
        self.assertEqual(pause["paused_status"], "paused", pause)
        self.assertEqual(pause["paused_items"].get("pending"), 2, pause)
        self.assertEqual(pause["paused_items"].get("completed", 0), 0, pause)
        self.assertEqual(pause["resumed_status"], "completed", pause)
        self.assertEqual(pause["resumed_items"].get("completed"), 2, pause)

    def test_new_job_blocked_while_paused(self):
        self.assertEqual(self.payload["duplicate_status"], 409, self.payload)

    def test_timeout_is_recorded_as_retryable(self):
        timeout = self.payload["timeout"]
        self.assertIn(timeout["job_status"], ("failed", "partially_failed"), timeout)
        self.assertEqual(timeout["stages"], ["model_response"], timeout)
        self.assertEqual(timeout["retryable"], [1], timeout)


if __name__ == "__main__":
    unittest.main()
