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
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "old.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class UpgradeSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_upgrade_worker.py")

    def test_old_batch_table_is_migrated(self):
        self.assertTrue(self.payload["has_owner"], self.payload)
        self.assertEqual(self.payload["batch_preserved"], 1, self.payload)
        self.assertEqual(self.payload["index"], 1, self.payload)

    def test_old_guest_is_downgraded(self):
        self.assertEqual(self.payload["guest_role"], "guest", self.payload)
        self.assertEqual(self.payload["migrations"], [1, 2, 3, 4, 5], self.payload)


if __name__ == "__main__":
    unittest.main()
