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


class IdentityAndMappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_identity_mapping_worker.py")

    def test_same_code_is_not_assumed_same_person(self):
        enrollment = self.payload["enrollment"]
        self.assertEqual(enrollment["a_count"], 1, enrollment)
        self.assertEqual(enrollment["b_count"], 1, enrollment)
        self.assertEqual(enrollment["a_status"], "unverified", enrollment)
        self.assertEqual(enrollment["b_status"], "unverified", enrollment)
        self.assertEqual(enrollment["same_student_id"], True, enrollment)
        self.assertNotEqual(enrollment["a_class"], enrollment["b_class"], enrollment)

    def test_identity_confirmation_is_explicit_and_trend_warns(self):
        trend = self.payload["trend_identity"]
        self.assertEqual(trend["issues"][0]["identity_status"], "distinct", trend)
        self.assertIn("身份尚未全部确认", trend["warning"])

    def test_mapping_reject_bumps_version_and_records_history(self):
        mapping = self.payload["mapping"]
        self.assertEqual(mapping["v1"], 1, mapping)
        self.assertEqual(mapping["v2"], 2, mapping)
        self.assertEqual(mapping["item_status"], "rejected", mapping)
        self.assertEqual(mapping["summary"]["rejected"], 1, mapping)
        self.assertEqual(mapping["history_actions"], ["confirm", "reject"], mapping)

    def test_impact_detects_stale_grades_and_analyses(self):
        impact = self.payload["impact"]
        self.assertEqual(impact["version"], 2, impact)
        self.assertGreaterEqual(impact["stale_grades"], 1, impact)
        self.assertGreaterEqual(impact["stale_analyses"], 1, impact)

    def test_mapping_change_creates_new_snapshot(self):
        snapshots = self.payload["snapshots"]
        self.assertTrue(snapshots["reused"], snapshots)
        self.assertTrue(snapshots["new_after_v1"], snapshots)
        self.assertTrue(snapshots["new_after_v2"], snapshots)

    def test_delete_batch_removes_only_its_enrollments(self):
        delete = self.payload["delete"]
        self.assertEqual(delete["remaining_a"], 0, delete)
        self.assertEqual(delete["remaining_b"], 1, delete)


if __name__ == "__main__":
    unittest.main()
