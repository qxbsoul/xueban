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


class QualityGateEnforcementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_quality_gate_http_worker.py")

    def test_gate_detects_blocking_issue(self):
        self.assertTrue(self.payload["gate_blocking"], self.payload)
        self.assertIn("missing_scoring_points", self.payload["gate_types"])

    def test_gate_no_longer_blocks_task_creation(self):
        # v1.5.19：质量闸门只作为报告提示，任务创建不再被阻断，也不需要 force/理由。
        self.assertEqual(self.payload["plain_status"], 200, self.payload)
        self.assertIsNone(self.payload["plain_code"], self.payload)

    def test_no_override_audit_row_is_written(self):
        self.assertEqual(self.payload["forced_status"], 200, self.payload)
        self.assertEqual(self.payload["override_count"], 0, self.payload)
        self.assertIsNone(self.payload["override_reason"], self.payload)

    def test_non_blocking_batch_can_start(self):
        self.assertEqual(self.payload["good_status"], 200, self.payload)


if __name__ == "__main__":
    unittest.main()
