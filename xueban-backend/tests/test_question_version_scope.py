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


class QuestionVersionScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_version_scope_worker.py")

    def test_selected_answers_use_own_version(self):
        selected = self.payload["selected"]
        self.assertEqual(selected["a_max"], 5.0, selected)
        self.assertEqual(selected["b_max"], 10.0, selected)
        self.assertEqual(selected["a_stem"], "题干-A", selected)
        self.assertEqual(selected["b_stem"], "题干-B", selected)
        self.assertEqual(selected["a_source"], "version", selected)

    def test_issue_detection_uses_own_version(self):
        self.assertEqual(self.payload["issues_a"]["by_type"], [], "A 的问题清单被其他批次的题面/满分污染")
        self.assertEqual(self.payload["issues_a"]["count"], 0, self.payload["issues_a"])
        # C 自己是非法满分 + 缺评分点，必须被识别
        types = {item["type"] for item in self.payload["issues_c"]["by_type"]}
        self.assertIn("invalid_max_score", types)
        self.assertIn("missing_scoring_points", types)

    def test_quality_gate_uses_own_version(self):
        self.assertFalse(self.payload["gate_a"]["blocking"], self.payload["gate_a"])
        self.assertNotIn("invalid_max_score", self.payload["gate_a"]["types"])
        self.assertEqual(self.payload["gate_a"]["sources"], {"version": 1, "global_fallback": 0})
        self.assertTrue(self.payload["gate_c"]["blocking"], self.payload["gate_c"])

    def test_results_and_details_use_own_version(self):
        results = self.payload["results_by_batch"]
        self.assertEqual(results["a"]["stem"], "题干-A", results)
        self.assertEqual(results["b"]["stem"], "题干-B", results)
        self.assertEqual(results["a"]["source"], "version")
        detail_a = self.payload["detail_a"]
        detail_b = self.payload["detail_b"]
        self.assertEqual(detail_a["stem"], "题干-A", detail_a)
        self.assertEqual(detail_b["stem"], "题干-B", detail_b)
        self.assertEqual(detail_a["source"], "version")
        self.assertEqual(detail_a["versions"], 1)
        self.assertEqual(self.payload["result_a"]["stem"], "题干-A", self.payload["result_a"])
        self.assertEqual(self.payload["student_stems"], ["题干-A", "题干-B", "题干-C"], self.payload["student_stems"])


if __name__ == "__main__":
    unittest.main()
