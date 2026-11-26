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


class FullFlowHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_full_flow_http_worker.py")

    def test_import_and_gate(self):
        payload = self.payload
        self.assertEqual(payload["import_counts"]["answers"], 2, payload)
        self.assertEqual(payload["credentials"], 2, payload)
        self.assertEqual(payload["confirm"], 200)
        self.assertFalse(payload["gate"]["blocking"], payload["gate"])

    def test_mapping_confirm_and_audit(self):
        self.assertEqual(self.payload["mapping_before"], ["legacy", "legacy"], self.payload)
        self.assertEqual(self.payload["link_status"], 200, self.payload)
        self.assertGreaterEqual(self.payload["audit_confirmed"], 1, self.payload)
        self.assertEqual(self.payload["method_link"], 200, self.payload)

    def test_mock_grading_job_completes(self):
        job = self.payload["job"]
        self.assertEqual(self.payload["grade_status"], 200, self.payload)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["processed"], 2, job)
        self.assertEqual(job["completed"], 2, job)
        self.assertEqual(self.payload["items"], {"count": 2, "completed": 2}, self.payload)
        self.assertEqual(self.payload["failures"], 0, self.payload)

    def test_analytics_endpoints(self):
        self.assertEqual(self.payload["classes"], 2, self.payload)
        self.assertEqual(self.payload["question_detail"], 200, self.payload)
        self.assertIsNotNone(self.payload["question_avg"], self.payload)
        self.assertEqual(self.payload["profile_status"], "S01", self.payload)
        self.assertGreaterEqual(self.payload["profile_evidence"], 1, self.payload)
        self.assertEqual(self.payload["compare_status"], 200, self.payload)

    def test_error_labels_and_recommendations(self):
        self.assertEqual(self.payload["classify_status"], 200, self.payload)
        self.assertGreaterEqual(self.payload["classify_count"], 1, self.payload)
        labels = {item["label"] for item in self.payload["labels"] or []}
        self.assertIn("运算错误", labels, labels)
        self.assertEqual(self.payload["recommend_status"], 200, self.payload)
        self.assertTrue(set(self.payload["recommendation_types"]) & {"knowledge_point", "method", "error_pattern", "prerequisite"}, self.payload)

    def test_knowledge_graph_and_cleanup(self):
        self.assertGreaterEqual(self.payload["graph_candidates"], 1, self.payload)
        self.assertGreaterEqual(self.payload["graph_edges"], 1, self.payload)
        self.assertEqual(self.payload["delete_status"], 200, self.payload)
        for table, count in self.payload["residual"].items():
            self.assertEqual(count, 0, f"删除后 {table} 仍有残留")


if __name__ == "__main__":
    unittest.main()
