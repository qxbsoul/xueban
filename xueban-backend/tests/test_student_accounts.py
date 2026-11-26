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


class StudentAccountFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_accounts_http_worker.py")

    def test_import_issues_initial_credentials(self):
        self.assertEqual(self.payload["credential_count"], 2, self.payload)
        self.assertTrue(self.payload["credential_has_password"])

    def test_student_can_login_and_only_sees_self(self):
        self.assertEqual(self.payload["login_status"], 200, self.payload)
        self.assertTrue(self.payload["login_must_change"])
        self.assertEqual(self.payload["me_status"], 200, self.payload)
        self.assertEqual(self.payload["me_student"], "S01")
        self.assertEqual(self.payload["other_student_status"], 403, self.payload)

    def test_first_login_password_change_is_enforced(self):
        self.assertEqual(self.payload["change_status"], 200, self.payload)
        self.assertEqual(self.payload["relogin_status"], 200, self.payload)
        self.assertFalse(self.payload["relogin_must_change"])
        self.assertEqual(self.payload["old_password_status"], 401, self.payload)

    def test_teacher_reset_issues_new_initial_password(self):
        self.assertEqual(self.payload["reset_status"], 200, self.payload)
        self.assertEqual(self.payload["reset_login_status"], 200, self.payload)
        self.assertTrue(self.payload["reset_must_change"])

    def test_self_registered_student_can_be_bound(self):
        self.assertTrue(self.payload["unbound_contains_self"], self.payload)
        self.assertEqual(self.payload["bind_status"], 200, self.payload)
        self.assertEqual(self.payload["self_me_status"], 200, self.payload)
        self.assertEqual(self.payload["self_me_student"], "S02")

    def test_password_never_stored_in_plaintext(self):
        self.assertTrue(self.payload["password_stored_hashed"], self.payload)
        self.assertIn("teacher_reset_password", self.payload["audit_actions"])


if __name__ == "__main__":
    unittest.main()
