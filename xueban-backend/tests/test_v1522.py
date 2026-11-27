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


class V1522AuditTest(unittest.TestCase):
    """v1.5.22：未作答/缺失分离、覆盖率明细、非关键复核自动归并。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_v1522_worker.py")

    def test_only_normal_pending_is_gradable(self):
        self.assertEqual(self.payload["pending"], ["v22_wait"], self.payload)

    def test_audit_distinguishes_unanswered_and_missing(self):
        audit = self.payload["audit"]
        self.assertEqual(audit["unanswered_answers"], 1, audit)
        self.assertEqual(audit["missing_answers"], 1, audit)
        self.assertEqual(audit["raw_answers"], 5, audit)

    def test_audit_does_not_call_missing_failure(self):
        audit = self.payload["audit"]
        self.assertEqual(audit["latest_grades"]["failed"], 0, audit)
        self.assertEqual(audit["latest_grades"]["review"], 1, audit)

    def test_normal_grade_enters_evidence(self):
        audit = self.payload["audit"]
        self.assertEqual(audit["evidence_rows"], 2, audit)
        self.assertEqual(audit["students_with_evidence"], 1, audit)

    def test_coverage_details_explain_low_valid_students(self):
        point = self.payload["profile"][0]
        self.assertEqual(point["students_with_any_evidence"], 1, point)
        self.assertEqual(point["valid_students"], 0, point)
        self.assertAlmostEqual(point["evidence_coverage"], 1 / 3, places=3)

    def test_noncritical_review_can_be_reclassified(self):
        data = self.payload["reclass"]
        self.assertEqual(data["auto"], 1, data)
        self.assertEqual(data["kept"], 0, data)
        self.assertIn("scoring_point_awarded_capped", data["reasons"], data)

    def test_unconfirmed_review_is_not_evidence(self):
        usage = self.payload["usage_review"]
        self.assertFalse(usage["score"], usage)
        self.assertFalse(usage["evidence"], usage)

    def test_student_dedup_in_coverage(self):
        point = self.payload["profile"][0]
        self.assertEqual(point["students_with_any_evidence"], 1, point)
        self.assertEqual(len(self.payload["pending"]), 1, self.payload)


if __name__ == "__main__":
    unittest.main()
