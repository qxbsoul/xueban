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


class StepStructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_structure_worker.py")

    def test_source_steps_have_offsets_and_stable_ids(self):
        source = self.payload["source"]
        self.assertEqual([item["source_step_id"] for item in source], ["src-1", "src-2"], source)
        text = "1. 2x+3=7；移项得2x=4\n2. x=2"
        for item in source:
            self.assertEqual(text[item["start_offset"]:item["end_offset"]], item["original_text"], item)

    def test_full_structure_passes_and_links_knowledge_pending(self):
        good = self.payload["good"]
        self.assertFalse(good["review"], good)
        self.assertEqual(good["source_ids"], ["src-1", "src-2"], good)
        self.assertEqual(good["knowledge"][0]["source_type"], "model_inferred", good)
        self.assertEqual(good["knowledge"][0]["confirmation_status"], "pending", good)
        self.assertEqual(good["validation"]["reasoning_step_count"], 2, good)
        self.assertEqual(good["validation"]["operation_unit_count"], 3, good)

    def test_source_mismatch_is_not_fabricated(self):
        unmapped = self.payload["unmapped"]
        self.assertIsNone(unmapped["source"], unmapped)
        self.assertTrue(unmapped["review"], unmapped)
        self.assertIn("reasoning_step_without_source", unmapped["issues"], unmapped)

    def test_same_point_multiple_evidence_allowed_once(self):
        multi = self.payload["multi"]
        self.assertEqual(multi["point"]["awarded"], 2.0, multi)
        self.assertEqual(multi["point"]["evidence_count"], 2, multi)

    def test_point_over_award_is_rejected(self):
        self.assertIn("invalid scoring point", self.payload["over_error"] or "", self.payload)
        self.assertIn("unknown scoring point", self.payload["unknown_error"] or "", self.payload)

    def test_operation_and_unaddressed_points_require_review(self):
        self.assertTrue(self.payload["op_mismatch"]["review"], self.payload)
        self.assertIn("operation_sum_mismatch", self.payload["op_mismatch"]["issues"], self.payload)
        self.assertTrue(self.payload["unaddressed"]["review"], self.payload)
        self.assertEqual(self.payload["unaddressed"]["missing"], ["Q001P1"], self.payload)


if __name__ == "__main__":
    unittest.main()
