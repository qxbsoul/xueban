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


class BackgroundReliabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_reliability_worker.py")

    def test_stale_job_is_recovered_to_paused(self):
        recover = self.payload["recover"]
        self.assertEqual(recover["jobs"], 1, recover)
        self.assertEqual(recover["items"], 1, recover)
        self.assertEqual(recover["status"], "paused", recover)
        self.assertEqual(recover["pause_requested"], 1, recover)
        self.assertEqual(recover["still_running"], 0, recover)

    def test_two_workers_do_not_double_grade(self):
        concurrency = self.payload["concurrency"]
        self.assertEqual(concurrency["grader_calls"], concurrency["item_count"], concurrency)
        self.assertEqual(concurrency["unique_calls"], concurrency["item_count"], concurrency)
        self.assertEqual(concurrency["job_status"], "completed", concurrency)
        self.assertEqual(concurrency["completed"], concurrency["item_count"], concurrency)
        self.assertEqual(concurrency["failed"], 0, concurrency)

    def test_processed_equals_completed_plus_failed(self):
        concurrency = self.payload["concurrency"]
        self.assertEqual(concurrency["processed"], concurrency["completed"] + concurrency["failed"], concurrency)
        self.assertEqual(concurrency["grade_rows"], concurrency["item_count"], concurrency)


if __name__ == "__main__":
    unittest.main()
