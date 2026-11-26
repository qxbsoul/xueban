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
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class MigrationAtomicityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_migration_atomic_worker.py")

    def test_roles_converge_and_unknown_is_not_teacher(self):
        roles = self.payload["first"]["roles"]
        self.assertEqual(roles["g1"], "guest", roles)
        self.assertEqual(roles["g2"], "guest", roles)
        self.assertEqual(roles["n1"], "guest", roles)
        self.assertEqual(roles["t1"], "teacher", roles)
        self.assertEqual(roles["s1"], "student", roles)
        self.assertEqual(roles["u1"], "admin", roles)

    def test_audit_matches_role_changes(self):
        self.assertEqual(self.payload["audit_delta"], 3, self.payload)
        self.assertEqual(self.payload["first"]["audit"], self.payload["before"]["audit"] + 3, self.payload)

    def test_v5_enrollment_backfill(self):
        enrollments = self.payload["first"]["enrollments"]
        self.assertEqual(len(enrollments), self.payload["expected_enrollments"], enrollments)
        self.assertTrue(all(row["identity_status"] == "unverified" for row in enrollments), enrollments)

    def test_migrations_are_versioned(self):
        self.assertEqual(self.payload["first"]["migrations"], [1, 2, 3, 4, 5], self.payload)
        self.assertEqual(self.payload["first"]["status"]["status"], "ready", self.payload)

    def test_repeat_bootstrap_is_idempotent(self):
        first, second = self.payload["first"], self.payload["second"]
        self.assertEqual(first["audit"], second["audit"], self.payload)
        self.assertEqual(len(first["enrollments"]), second["enrollments"], self.payload)
        self.assertEqual(first["roles"], second["roles"], self.payload)
        self.assertEqual(first["migrations"], second["migrations"], self.payload)


class MigrationFailureRollbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_migration_failure_worker.py")

    def test_partial_v4_changes_are_rolled_back(self):
        payload = self.payload
        self.assertEqual(payload["after_failure"]["roles"], payload["before"]["roles"], payload)
        self.assertEqual(payload["after_failure"]["audit"], payload["before"]["audit"], payload)
        self.assertNotIn(4, payload["after_failure"]["migrations"], payload)
        self.assertEqual(payload["after_failure"]["state"]["status"], "failed", payload)
        self.assertEqual(payload["after_failure"]["failures"][0]["version"], 4, payload)

    def test_error_is_sanitized(self):
        message = self.payload["failure"]
        self.assertNotIn("SuperSecret", message)
        self.assertNotIn("sk-abc123456789", message)
        self.assertIn("sk-***", message)

    def test_status_endpoint_and_business_block(self):
        payload = self.payload
        self.assertEqual(payload["status_http"]["status_code"], 200, payload)
        self.assertEqual(payload["status_http"]["body"]["status"], "failed", payload)
        self.assertEqual(payload["status_http"]["body"]["failed_version"], 4, payload)
        self.assertEqual(payload["blocked_http"]["status_code"], 503, payload)
        self.assertEqual(payload["blocked_http"]["body"]["code"], "MIGRATION_NOT_READY", payload)
        self.assertTrue(payload["startup_failed"], payload)

    def test_retry_completes_and_records_once(self):
        retry = self.payload["after_retry"]
        self.assertEqual(retry["migrations"], [1, 2, 3, 4, 5], retry)
        self.assertEqual(retry["status"]["status"], "ready", retry)
        self.assertEqual(retry["audit"], self.payload["before"]["audit"] + 1, retry)
        self.assertEqual(retry["roles"]["g1"], "guest", retry)


if __name__ == "__main__":
    unittest.main()
