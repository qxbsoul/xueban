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


class ImportIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_import_worker.py")

    def test_cross_batch_same_event_kept(self):
        self.assertEqual(self.payload["a_counts"]["answers"], 1, self.payload)
        self.assertEqual(self.payload["b_counts"]["answers"], 1, "第二批次相同 event/revision 被跳过")
        self.assertEqual(self.payload["b_counts"]["skipped"], 0, self.payload)
        self.assertEqual(self.payload["a_answer"]["original_answer"], "A 的答案")
        self.assertEqual(self.payload["b_answer"]["original_answer"], "B 的答案")

    def test_reimport_creates_new_task_without_skip(self):
        self.assertEqual(self.payload["c_counts"]["answers"], 1, self.payload)
        self.assertEqual(self.payload["c_counts"]["skipped"], 0, self.payload)
        self.assertEqual(self.payload["same_event_answers"], 3, self.payload)

    def test_versions_stay_isolated(self):
        self.assertEqual(self.payload["a_version"]["max_score"], 5.0)
        self.assertEqual(self.payload["b_version"]["max_score"], 10.0)
        self.assertNotEqual(self.payload["a_version"]["stem"], self.payload["b_version"]["stem"])

    def test_same_batch_duplicate_is_skipped(self):
        self.assertEqual(self.payload["fixed_first"]["answers"], 1, self.payload)
        self.assertEqual(self.payload["fixed_first"]["skipped"], 1, self.payload)
        self.assertEqual(self.payload["fixed_rows"], 1)
        self.assertEqual(len(self.payload["fixed_errors"]), 1, "去重原因必须被保留")
        self.assertEqual(self.payload["fixed_errors"][0]["event_id"], "E-DUP")

    def test_in_file_duplicate_is_blocked_by_validation(self):
        self.assertEqual(self.payload["validate_duplicate_errors"], 1, self.payload)
        self.assertEqual(self.payload["blocked_status"], 422, self.payload)


if __name__ == "__main__":
    unittest.main()
