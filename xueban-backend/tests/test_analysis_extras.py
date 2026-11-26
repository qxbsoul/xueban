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


class ExtrasTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_extras_worker.py")

    def test_candidate_edges_are_marked_candidate(self):
        graph = self.payload["graph"]
        self.assertEqual(graph["generated"], 1, graph)
        self.assertEqual(graph["before_edges"]["candidate"], 1, graph)
        self.assertEqual(graph["before_edges"]["confirmed"], 0, graph)

    def test_edges_reject_duplicates_and_cycles(self):
        graph = self.payload["graph"]
        self.assertEqual(graph["edge_status"], "confirmed")
        self.assertIn("已存在", graph["duplicate_error"])
        self.assertIn("循环", graph["cycle_error"])

    def test_dependencies_expose_direct_prerequisites(self):
        self.assertEqual(self.payload["graph"]["dependencies"], ["kp:I1"], self.payload)

    def test_prerequisite_gap_detection(self):
        gaps = self.payload["gaps"]
        self.assertEqual(len(gaps), 1, gaps)
        self.assertEqual(gaps[0]["kp"], "kp:I2")
        self.assertEqual(gaps[0]["weak"], ["kp:I1"])

    def test_rule_based_error_classification_uses_step_evidence(self):
        classify = self.payload["classify"]
        self.assertIn("运算错误", classify["labels"], classify)
        self.assertIn("符号表达错误", classify["labels"], classify)
        self.assertEqual(classify["source"], "rule")
        self.assertEqual(classify["prompt_version"], "error-rule-v1")
        self.assertTrue(classify["has_evidence"], classify)

    def test_teacher_override_replaces_labels(self):
        teacher = self.payload["teacher_labels"]
        self.assertEqual(teacher["source"], "teacher")
        self.assertEqual(sorted(teacher["labels"]), ["概念理解错误", "步骤遗漏"])

    def test_error_summary_counts_answers(self):
        summary = {item["label"]: item for item in self.payload["summary"]}
        self.assertIn("概念理解错误", summary, self.payload["summary"])
        self.assertGreaterEqual(summary["概念理解错误"]["answer_count"], 1)

    def test_recommendations_are_explainable(self):
        items = self.payload["recommendations"]
        types = {item["type"] for item in items}
        self.assertIn("knowledge_point", types, items)
        self.assertIn("error_pattern", types, items)
        self.assertIn("prerequisite", types, items)
        self.assertTrue(all(item["questions"] or item["type"] != "knowledge_point" for item in items), items)

    def test_multi_class_compare_marks_missing_class_as_insufficient(self):
        compare = self.payload["compare"]
        classes = {row["class_id"]: row for row in compare["classes"]}
        self.assertEqual(classes["class_a"]["valid_student_count"], 1, classes)
        self.assertEqual(classes["class_b"]["valid_student_count"], 0, classes)
        self.assertIsNone(classes["class_b"]["overall_mastery"], classes)
        self.assertFalse(classes["class_b"]["has_evidence"], classes)
        self.assertGreaterEqual(compare["insufficient_values"], 2, compare)


if __name__ == "__main__":
    unittest.main()
