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


class ConfirmationSemanticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_confirmation_worker.py")

    def test_partial_point_confirmation_does_not_mark_whole_grade(self):
        first = self.payload["first"]
        self.assertFalse(first["fully"], first)
        self.assertEqual(first["status"], "needs_review", first)
        self.assertEqual(first["confirmed"], 1, first)
        self.assertEqual(first["total"], 2, first)

    def test_all_points_confirmed_then_manual_confirmed(self):
        self.assertTrue(self.payload["second"]["fully"], self.payload)
        self.assertEqual(self.payload["second"]["status"], "manual_confirmed", self.payload)

    def test_manual_override_records_point_sync_state(self):
        manual = self.payload["manual"]
        self.assertEqual(manual["status"], "manual_corrected", manual)
        self.assertFalse(manual["override"]["points_synced"], manual)

    def test_structure_review_blocks_whole_confirm(self):
        blocked = self.payload["blocked"]
        self.assertEqual(blocked["status_code"], 409, blocked)
        self.assertEqual(blocked["detail"]["code"], "STRUCTURE_REVIEW_REQUIRED", blocked)


if __name__ == "__main__":
    unittest.main()
