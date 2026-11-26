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
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class QualityAndAttemptTest(unittest.TestCase):
    def test_quality_gate_blocks_orphan_answer(self):
        payload = run_worker("_quality_gate_worker.py")
        self.assertTrue(payload["blocking"])
        self.assertIn("orphan_student", payload["types"])
        self.assertIn("orphan_question", payload["types"])

    def test_attempt_records_scope_and_retryable(self):
        payload = run_worker("_attempt_worker.py")
        self.assertEqual(payload["batch_id"], "batch_attempt")
        self.assertEqual(payload["student_id"], "student_attempt")
        self.assertEqual(payload["retryable"], 1)
        self.assertGreaterEqual(payload["duration_ms"], 0)

    def test_quality_gate_blocks_bad_question_rules(self):
        payload = run_worker("_quality_full_worker.py")
        self.assertTrue(payload["blocking"])
        self.assertIn("invalid_max_score", payload["types"])
        self.assertIn("missing_reference_answer", payload["types"])
        self.assertIn("missing_scoring_points", payload["types"])

    def test_json_parse_failure_is_retryable(self):
        env = dict(os.environ)
        with tempfile.TemporaryDirectory() as tmp:
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("_attempt_worker.py")), "json"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["retryable"], 1)


if __name__ == "__main__":
    unittest.main()
