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


class DataQualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_data_quality_worker.py")

    def test_primary_status_is_mutually_exclusive_and_totals_match(self):
        summary = self.payload["summary"]
        self.assertEqual(summary["total"], 4, summary)
        self.assertEqual(summary["valid"] + summary["abnormal"] + summary["pending_review"] + summary["excluded"], summary["total"], summary)
        self.assertEqual(summary["valid"], 1, summary)
        self.assertEqual(summary["abnormal"], 1, summary)
        self.assertEqual(summary["pending_review"], 2, summary)

    def test_issue_types_are_multilabel_but_not_double_counted(self):
        records = {row["answer_id"]: row for row in self.payload["items"]}
        self.assertEqual(records["a1"]["primary_status"], "abnormal", records)
        self.assertEqual(records["a2"]["primary_status"], "pending_review", records)
        self.assertEqual(self.payload["issue_counts"]["score_out_of_range"], 1, self.payload)


if __name__ == "__main__":
    unittest.main()
