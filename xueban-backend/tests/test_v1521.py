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
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class V1521ReliabilityTest(unittest.TestCase):
    """v1.5.21：HTTP 12 类错误、恢复重试、并发领取、暂停恢复、重启、幂等。"""

    @classmethod
    def setUpClass(cls):
        cls.http = run_worker("_v1521_http_worker.py")["results"]
        cls.queue = run_worker("_v1521_queue_worker.py")

    def test_http_success(self):
        data = self.http["success"]
        self.assertEqual(data["status"], "completed", data)
        self.assertEqual(len(data["runs"]), 1, data)

    def test_read_timeout_recovers(self):
        data = self.http["read_timeout"]
        self.assertEqual(data["status"], "completed", data)
        self.assertGreaterEqual(data["attempt_count"], 2, data)
        self.assertIn("network_timeout", [item["error_type"] for item in data["attempts"]], data)

    def test_incomplete_response_recovers(self):
        data = self.http["incomplete"]
        self.assertEqual(data["status"], "completed", data)
        self.assertIn("network_incomplete_read", [item["error_type"] for item in data["attempts"]], data)

    def test_ssl_disconnect_recovers(self):
        data = self.http["ssl_recover"]
        self.assertEqual(data["status"], "completed", data)
        self.assertIn("network_ssl_eof", [item["error_type"] for item in data["attempts"]], data)

    def test_http_429_recovers(self):
        data = self.http["rate_limit"]
        self.assertEqual(data["status"], "completed", data)
        self.assertIn("rate_limit", [item["error_type"] for item in data["attempts"]], data)

    def test_http_500_recovers(self):
        data = self.http["server_500"]
        self.assertEqual(data["status"], "completed", data)
        self.assertIn("http_error", [item["error_type"] for item in data["attempts"]], data)

    def test_bad_envelope_is_final_failure(self):
        data = self.http["bad_envelope"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["last_error_type"], "RESPONSE_VALIDATION_ERROR", data)

    def test_truncated_model_json_is_failure(self):
        data = self.http["incomplete_json"]
        self.assertEqual(data["status"], "failed", data)
        self.assertIn("model_output_truncated", [item["error_type"] for item in data["attempts"]], data)

    def test_scoring_mismatch_is_capped_to_review(self):
        data = self.http["bad_scoring"]
        self.assertEqual(data["status"], "completed", data)
        self.assertEqual(data["runs"][0]["status"], "graded", data)

    def test_null_score_is_data_failure(self):
        data = self.http["null_score"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["last_error_type"], "DATA_VALIDATION_ERROR", data)

    def test_single_failure_does_not_stop_queue(self):
        data = self.queue["single_failure"]
        self.assertEqual(data["completed"], 3, data)
        self.assertEqual(data["failed"], 1, data)

    def test_concurrency_no_duplicate_claims(self):
        for level in (2, 3, 5, 8):
            data = self.queue[f"stress_{level}"]
            self.assertEqual(data["completed"], 12, data)
            self.assertEqual(data["failed"], 0, data)
            self.assertEqual(data["duplicate_calls"], 0, data)
            self.assertGreaterEqual(data["max_active"], 2, data)

    def test_pause_resume_breakpoint(self):
        data = self.queue["pause_resume"]
        self.assertEqual(data["paused"], "paused", data)
        self.assertGreater(data["pending"], 0, data)
        self.assertEqual(data["resumed"], "completed", data)

    def test_restart_recovery(self):
        data = self.queue["restart"]
        self.assertGreaterEqual(data["recovered"], 1, data)
        self.assertEqual(data["status"], "completed", data)

    def test_retry_only_failed_does_not_touch_success(self):
        data = self.queue["retry_only_failed"]
        items = {item["answer_id"]: item for item in data["items"]}
        self.assertEqual(data["result"]["retried"], 1, data)
        self.assertEqual(items["ans_retry_0"]["status"], "completed", data)
        self.assertEqual(items["ans_retry_0"]["grade_id"], "existing_success", data)
        self.assertEqual(items["ans_retry_1"]["status"], "pending", data)

    def test_network_retry_produces_one_valid_result(self):
        data = self.queue["one_valid_result"]
        self.assertEqual(data["runs"], 1, data)
        self.assertEqual(data["valid"], 1, data)
        self.assertEqual(data["calls"], 2, data)

    def test_attempts_and_error_fields_persisted(self):
        data = self.http["read_timeout"]
        self.assertGreaterEqual(data["attempt_count"], 2, data)
        self.assertTrue(data["result_id"], data)
        self.assertTrue(data["runs"][0]["id"], data)


if __name__ == "__main__":
    unittest.main()
