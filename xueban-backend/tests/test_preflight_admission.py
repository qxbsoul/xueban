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
    env.pop("DEEPSEEK_API_KEY", None)
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class PreflightAdmissionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_preflight_worker.py")

    def test_issue_is_scoped_to_student(self):
        anomalies = self.payload["anomalies"]
        self.assertEqual(len(anomalies), 1, anomalies)
        self.assertEqual(anomalies[0]["student"], "S01", anomalies)

    def test_dotenv_key_is_detected_without_leaking(self):
        self.assertEqual(self.payload["with_env"]["model"], "configured", self.payload)
        self.assertEqual(self.payload["without_env"]["model"], "not_configured", self.payload)

    def test_incomplete_migration_is_blocking(self):
        migration = self.payload["with_env"]["migration"]
        self.assertFalse(migration["complete"], migration)
        self.assertIn("database_not_migrated_v1_v5", self.payload["with_env"]["blocking"], self.payload)

    def test_preflight_is_read_only(self):
        self.assertTrue(self.payload["read_only"], self.payload)
        self.assertEqual(self.payload["sha_before"], self.payload["sha_after"], self.payload)


if __name__ == "__main__":
    unittest.main()
