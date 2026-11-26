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


class FeedbackAndMetricsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_feedback_metrics_worker.py")

    def test_error_labels_are_classified(self):
        labels = self.payload["rule_labels"]
        self.assertEqual(len(labels), 2, labels)
        for values in labels.values():
            self.assertTrue(values, labels)
            self.assertIn(values[0], ("概念理解错误", "公式使用错误", "运算错误", "推理错误", "步骤遗漏", "条件遗漏", "方法选择不当", "符号表达错误", "单位或格式错误", "无法判断"), labels)

    def test_precision_recall_use_manual_gold(self):
        metrics = self.payload["metrics"]
        self.assertEqual(metrics["answers"], 2, metrics)
        self.assertTrue(any(item["f1"] == 1.0 for item in metrics["labels"].values()), metrics)
        for item in metrics["labels"].values():
            if item["gold"]:
                self.assertIsNotNone(item["recall"], metrics)
            if item["predicted"]:
                self.assertIsNotNone(item["precision"], metrics)

    def test_recommendation_feedback_closes_the_loop(self):
        feedback = self.payload["feedback"]
        self.assertEqual(feedback["status"], "planned", feedback)
        self.assertEqual(feedback["completed"], "completed", feedback)
        self.assertTrue(feedback["followup"]["practice_completed"], feedback)
        self.assertEqual(feedback["listed"], 1, feedback)


if __name__ == "__main__":
    unittest.main()
