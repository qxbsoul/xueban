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
    env["XUEBAN_GRADER_RETRY_BASE_MS"] = "10"
    env["XUEBAN_GRADER_ATTEMPTS"] = "3"
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class GradingReliabilityTest(unittest.TestCase):
    """任务书第十四节 1–6：中断恢复、幂等、并发领取、暂停恢复、重试、成功不重判。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_reliability_worker_v2.py")

    def test_network_error_is_retried_then_succeeds(self):
        data = self.payload["retried"]
        self.assertEqual(data["status"], "graded", data)
        self.assertEqual(data["calls"], 2, data)
        stages = [item["failure_stage"] for item in data["attempts"]]
        self.assertIn("model_response", stages, data)
        self.assertEqual(data["attempts"][0]["error_type"], "network_incomplete_read", data)
        self.assertGreaterEqual(data["attempts"][0]["duration_ms"], 0, data)

    def test_contract_violation_gets_one_targeted_retry(self):
        # v1.5.20：结构错误允许一次针对性纠错重试；仍然失败才落 failed，不能无限调用模型。
        data = self.payload["invalid"]
        self.assertEqual(data["status"], "failed", data)
        self.assertEqual(data["attempts"], 2, data)
        self.assertEqual(data["error_type"], "scoring_point_max_mismatch", data)

    def test_successful_answer_is_not_graded_twice(self):
        data = self.payload["idempotent"]
        self.assertTrue(data["reused"], data)
        self.assertEqual(data["versions"], 1, data)

    def test_atomic_claim_lets_exactly_one_worker_win(self):
        self.assertEqual(self.payload["claim"]["wins"], 1, self.payload["claim"])

    def test_expired_lease_is_reclaimed(self):
        data = self.payload["reclaim"]
        self.assertEqual(data["items"], 1, data)
        self.assertEqual(data["status"], "pending", data)

    def test_stale_run_is_recovered_to_paused(self):
        data = self.payload["recover"]
        self.assertEqual(data["job"]["status"], "paused", data)
        self.assertEqual(data["job"]["pause_requested"], 1, data)
        self.assertEqual(data["item"], "pending", data)

    def test_paused_job_does_not_claim_new_items(self):
        data = self.payload["pause"]
        self.assertEqual(data["job"], "paused", data)
        self.assertEqual(data["item"], "pending", data)


if __name__ == "__main__":
    unittest.main()
