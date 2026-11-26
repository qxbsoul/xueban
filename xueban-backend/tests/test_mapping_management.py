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


class MappingManagementTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_mapping_worker.py")

    def test_legacy_mapping_is_not_reported_as_confirmed(self):
        self.assertEqual(self.payload["before_status"], "legacy", self.payload)

    def test_teacher_confirmation_marks_confirmed_and_normalizes_weights(self):
        self.assertEqual(self.payload["confirmed_status"], "confirmed", self.payload)
        self.assertAlmostEqual(self.payload["weights"]["kp:I01"], 0.75, places=6)
        self.assertAlmostEqual(self.payload["weights"]["kp:I02"], 0.25, places=6)
        self.assertAlmostEqual(self.payload["weight_sum"], 1.0, places=6)

    def test_mapping_change_writes_history(self):
        self.assertGreaterEqual(self.payload["history_count"], 1, self.payload)
        self.assertEqual(self.payload["history_operator"], "teacher")

    def test_audit_counts_confirmed_and_legacy(self):
        audit = self.payload["audit"]
        self.assertGreaterEqual(audit["confirmed"], 1, audit)
        self.assertEqual(audit["illegal"], 0, audit)
        self.assertEqual(audit["methods"], 2, audit)
        self.assertEqual(audit["with_methods"], 1, audit)

    def test_confirmed_mapping_drives_evidence_weights(self):
        weights = self.payload["evidence_weights"]
        self.assertAlmostEqual(weights["kp:I01"], 0.75, places=6)
        self.assertAlmostEqual(weights["kp:I02"], 0.25, places=6)
        self.assertEqual(self.payload["evidence_mapping_status"], ["confirmed"], self.payload)

    def test_methods_are_tracked_separately(self):
        methods = {item["name"]: item for item in self.payload["methods"]}
        self.assertEqual(set(methods), {"代入消元法", "加减消元法"}, self.payload["methods"])
        self.assertAlmostEqual(methods["代入消元法"]["weight"], 0.75, places=3)
        self.assertAlmostEqual(methods["加减消元法"]["weight"], 0.25, places=3)
        self.assertEqual(self.payload["method_link_count"], 2)

    def test_scoring_point_confirmation_history_has_versions_and_before_after(self):
        history = self.payload["confirm_history"]
        self.assertEqual(len(history), 2, history)
        self.assertEqual([item["version"] for item in history], [1, 2], history)
        self.assertEqual(history[1]["before"]["confirmed_score"], 4.0, history[1])
        self.assertEqual(history[1]["after"]["confirmed_score"], 3.0, history[1])


if __name__ == "__main__":
    unittest.main()
