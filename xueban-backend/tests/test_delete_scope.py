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


class DeleteBatchScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_delete_scope_worker.py")

    def test_all_delete_scenarios_leave_no_residual(self):
        for name, residual in self.payload["deleted"].items():
            for table, count in residual.items():
                self.assertEqual(count, 0, f"场景 {name} 删除后 {table} 仍有残留：{residual}")

    def test_other_batch_is_intact(self):
        keep = self.payload["keep"]
        self.assertEqual(keep["answers"], 1, keep)
        self.assertEqual(keep["grades"], 2, keep)
        self.assertEqual(keep["confirmations"], 1, keep)
        self.assertEqual(keep["corrections"], 1, keep)
        self.assertEqual(keep["jobs"], 1, keep)
        self.assertEqual(keep["versions"], 1, keep)

    def test_running_job_blocks_delete(self):
        self.assertEqual(self.payload["running_error"], 409, self.payload)


if __name__ == "__main__":
    unittest.main()
