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
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class PermissionMatrixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_permissions_http_worker.py")

    def test_guest_role_is_explicit_and_least_privilege(self):
        self.assertEqual(self.payload["guest_role"], "guest", self.payload)
        matrix = self.payload["matrix"]["guest"]
        for endpoint in ("batches", "summary", "students", "answers", "analysis_classes", "real_batch", "real_answer", "analysis_me", "delete_batch"):
            self.assertEqual(matrix[endpoint], 403, f"guest {endpoint} 应被拒绝: {matrix}")

    def test_legacy_guest_and_null_role_are_downgraded_without_promoting_teacher(self):
        self.assertEqual(self.payload["legacy_roles"]["legacy_guest"], "guest", self.payload)
        self.assertEqual(self.payload["legacy_roles"]["legacy_null"], "guest", self.payload)
        self.assertGreaterEqual(self.payload["audit_count"], 2, self.payload)

    def test_guest_password_login_is_rejected(self):
        self.assertEqual(self.payload["matrix"]["guest"]["password_login"], 401, self.payload)

    def test_teacher_cannot_access_other_teacher_resources(self):
        matrix = self.payload["matrix"]
        self.assertEqual(matrix["teacher_a"]["batch_b_detail"], 404, matrix)
        self.assertEqual(matrix["teacher_b"]["batch_a_detail"], 404, matrix)
        self.assertEqual(matrix["teacher_a"]["answer_b"], 404, matrix)
        self.assertEqual(matrix["teacher_b"]["grade_a"], 404, matrix)
        self.assertEqual(matrix["teacher_a"]["student_b"], 404, matrix)
        self.assertEqual(matrix["teacher_a"]["analysis_student_b"], 404, matrix)
        self.assertEqual(matrix["teacher_b"]["scoring_point_a"], 404, matrix)
        self.assertEqual(matrix["teacher_a"]["scoring_point_a"], 200, matrix)

    def test_teacher_can_access_own_and_legacy(self):
        matrix = self.payload["matrix"]["teacher_a"]
        self.assertEqual(matrix["own_batch"], 200, matrix)
        self.assertEqual(matrix["legacy_batch"], 200, matrix)

    def test_batch_list_is_scoped(self):
        counts = self.payload["counts"]
        own = counts["own_batches"]
        self.assertIn(own["a"], counts["teacher_a_batches"])
        self.assertNotIn(own["b"], counts["teacher_a_batches"])
        self.assertIn(own["b"], counts["teacher_b_batches"])
        self.assertNotIn(own["a"], counts["teacher_b_batches"])

    def test_student_teacher_endpoints_rejected(self):
        matrix = self.payload["matrix"]["student_x"]
        self.assertEqual(matrix["batches"], 403, matrix)
        self.assertEqual(matrix["students"], 403, matrix)
        self.assertEqual(matrix["analysis_classes"], 403, matrix)
        self.assertEqual(matrix["teacher_batch"], 403, matrix)
        self.assertEqual(matrix["other_student_analysis"], 403, matrix)
        self.assertEqual(matrix["own_me"], 200, matrix)

    def test_unauthenticated_requests_are_401(self):
        matrix = self.payload["matrix"]["anon"]
        self.assertEqual(matrix["batches"], 401, matrix)
        self.assertEqual(matrix["summary"], 401, matrix)
        self.assertEqual(matrix["answers"], 401, matrix)


if __name__ == "__main__":
    unittest.main()
