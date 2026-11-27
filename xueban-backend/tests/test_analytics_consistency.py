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


class AnalyticsConsistencyTest(unittest.TestCase):
    """任务书第十四节 14/16/21/22：跨任务隔离、任意班级比较、错误分类幂等、依赖图完整性。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_analytics_consistency_worker.py")

    def test_any_class_combination_can_be_compared(self):
        data = self.payload["compare"]
        self.assertEqual(data["all_classes"], 3, data)
        self.assertEqual(data["two_classes"], ["C1", "C3"], data)
        self.assertEqual(data["single_class"], ["C2"], data)
        self.assertGreaterEqual(data["all_kp_rows"], 1, data)
        row = data["first_row"]
        self.assertIsNotNone(row, data)
        self.assertEqual(len(row["values"]), 2, row)
        self.assertIsNotNone(row["difference"], row)

    def test_error_classification_is_idempotent(self):
        data = self.payload["labels"]
        first = [item["label"] for item in data["first"]]
        second = [item["label"] for item in data["second"]]
        stored = [item["label"] for item in data["stored"]]
        self.assertTrue(first, data)
        self.assertEqual(first, second, data)
        self.assertEqual(data["rows_first"], 1, data)
        self.assertEqual(data["rows_second"], 1, data)
        self.assertIn("概念理解错误", stored, data)
        self.assertTrue(data["clean_no_error"], data)
        self.assertEqual(data["clean_rows"], 0, data)

    def test_knowledge_graph_rejects_self_cycle_duplicate_and_unknown(self):
        data = self.payload["graph"]
        self.assertEqual(data["errors"]["self"], "知识点不能依赖自己", data)
        self.assertEqual(data["errors"]["cycle"], "该依赖会形成循环", data)
        self.assertEqual(data["errors"]["duplicate"], "依赖关系已存在", data)
        self.assertEqual(data["errors"]["unknown"], "依赖节点必须是当前任务涉及的知识点", data)
        self.assertEqual(data["edges"], 2, data)
        self.assertEqual(data["confirmed"], 2, data)
        self.assertEqual(data["direct"], ["kp:I02"], data)
        self.assertIn("kp:I01", data["transitive"], data)

    def test_edge_delete_updates_graph(self):
        data = self.payload["graph"]
        self.assertTrue(data["deleted"], data)
        self.assertEqual(data["after_delete"], 1, data)

    def test_cross_task_evidence_isolation(self):
        data = self.payload["isolation"]
        self.assertEqual(data["single_tasks"], ["batch_c1"], data)
        self.assertEqual(data["multi_tasks"], ["batch_c1", "batch_c2"], data)
        self.assertGreater(data["multi_rows"], data["single_rows"], data)


if __name__ == "__main__":
    unittest.main()
