import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("_delete_batch_worker.py")


class DeleteBatchTest(unittest.TestCase):
    def test_delete_removes_related_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, str(WORKER)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["deleted"])
            for key in ("answers", "jobs", "items", "grades", "attempts", "confirmations", "analysis", "batches"):
                self.assertEqual(payload[key], 0, key)


if __name__ == "__main__":
    unittest.main()
