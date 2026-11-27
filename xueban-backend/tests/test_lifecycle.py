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


class LifecycleTest(unittest.TestCase):
    """v1.5.19：统一计数/暂停恢复/重判/预处理对账。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_lifecycle_worker.py")

    def test_counts_are_mutually_exclusive(self):
        data = self.payload["counts"]
        self.assertEqual(data["total"], 5, data)
        self.assertEqual(data["success"], 1, data)
        self.assertEqual(data["review"], 1, data)
        self.assertEqual(data["failed"], 1, data)
        self.assertEqual(data["pending"], 1, data)
        self.assertEqual(data["running"], 1, data)
        self.assertEqual(data["completed"], 3, data)
        self.assertTrue(data["consistent"], data)

    def test_job_progress_and_state(self):
        data = self.payload["job"]
        self.assertEqual(data["progress"], 60.0, data)
        self.assertTrue(data["consistent"], data)
        self.assertEqual(data["state"], "已完成", data)

    def test_pause_and_resume_are_idempotent(self):
        data = self.payload["resume"]
        self.assertEqual(self.payload["pause"]["p1"], "paused", data)
        self.assertEqual(self.payload["pause"]["p2"], "paused", data)
        self.assertEqual(data["r1"], "queued", data)
        self.assertTrue(data["r2"], data)
        self.assertIn(data["r2_status"], ("queued", "running"), data)

    def test_retry_progress_without_new_job(self):
        data = self.payload["retry"]
        self.assertEqual(data["r1"], "retrying", data)
        self.assertEqual(data["retry_total"], 1, data)
        self.assertTrue(data["r2_reused"], data)
        self.assertEqual(data["item"]["status"], "pending", data)
        self.assertEqual(data["item"]["retry_round"], 1, data)

    def test_preprocess_states_reconcile(self):
        data = self.payload["preprocess"]
        self.assertEqual(data["raw"], 5, data)
        self.assertEqual(data["sum"], data["raw"], data)
        self.assertTrue(data["reconciled"], data)
        self.assertEqual(data["valid"] + data["anomaly"] + data["pending"], data["raw"], data)

    def test_process_requirement_levels(self):
        data = self.payload["preprocess"]
        self.assertEqual(data["requirements"].get("requires_process"), 2, data)
        self.assertEqual(data["requirements"].get("answer_only"), 2, data)
        self.assertEqual(data["requirements"].get("unknown"), 1, data)
        self.assertEqual(data["reminders"], 1, data)
        self.assertGreaterEqual(data["missing_process"], 2, data)


if __name__ == "__main__":
    unittest.main()
