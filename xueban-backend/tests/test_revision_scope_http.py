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


class RevisionScopeHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_revisions_http_worker.py")

    def test_http_status(self):
        self.assertEqual(self.payload["status"], {"a": 200, "b": 200, "ra": 200, "rb": 200}, self.payload)

    def test_answer_detail_revisions_are_batch_scoped(self):
        self.assertEqual(len(self.payload["a_revisions"]), 2, self.payload["a_revisions"])
        self.assertEqual(len(self.payload["b_revisions"]), 1, self.payload["b_revisions"])

    def test_result_detail_revisions_are_batch_scoped(self):
        self.assertEqual(len(self.payload["a_result_revisions"]), 2, self.payload["a_result_revisions"])
        self.assertEqual(len(self.payload["b_result_revisions"]), 1, self.payload["b_result_revisions"])

    def test_question_source_marked(self):
        self.assertEqual(self.payload["a_source"], "version")
        self.assertEqual(self.payload["b_source"], "version")


if __name__ == "__main__":
    unittest.main()
