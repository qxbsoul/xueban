import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_job_state_worker.py")


class JobStateTest(unittest.TestCase):
    def test_retry_keeps_job_id_and_pause_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            env["DEEPSEEK_API_KEY"] = "test-placeholder"
            result = subprocess.run([sys.executable, str(WORKER)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(payload["retry_job_id"], "job_same")
            self.assertEqual(payload["failed_status"], "pending")
            self.assertEqual(payload["paused_status"], "paused")
            self.assertEqual(payload["duplicate_error"], 409)


if __name__ == "__main__":
    unittest.main()
