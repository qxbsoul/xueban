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


class EvidenceAggregationTest(unittest.TestCase):
    """任务书第七项：同题多评分点映射同一知识点先聚合；修订只取当前版本。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_evidence_agg_worker.py")

    def test_same_question_points_count_as_one_unit(self):
        data = self.payload["one_question"]
        self.assertEqual(data["evidence_point_count"], 2, data)
        self.assertEqual(data["evidence_count"], 1, data)
        self.assertEqual(data["covered_questions"], 1, data)
        self.assertEqual(data["status"], "证据不足", data)

    def test_second_question_makes_two_units(self):
        data = self.payload["two_questions"]
        self.assertEqual(data["evidence_count"], 2, data)
        self.assertEqual(data["evidence_point_count"], 3, data)
        self.assertNotEqual(data["status"], "证据不足", data)
        self.assertAlmostEqual(data["weight"], 2.0, places=2, msg=data)

    def test_revision_uses_latest_version(self):
        data = self.payload["one_question"]
        # r2：P1 得 1 分、P2 得 0 分 → 该题得分率 0.5；若错误地取 r1 会是 0
        self.assertAlmostEqual(data["mastery"], 0.5, places=2, msg=data)
        self.assertEqual(self.payload["evidence_rows"], 6)

    def test_low_confidence_error_label_is_not_counted(self):
        data = self.payload["low_confidence"]
        self.assertEqual(data["rows"], 1, data)
        self.assertEqual(data["labels"], ["未识别错误"], data)
        self.assertTrue(data["raw_saved"], data)
        self.assertNotIn("无法确定", self.payload["error_frequency"]["types"], self.payload)

    def test_error_frequency_levels(self):
        data = self.payload["error_frequency"]
        calculation = data["calculation"]
        self.assertIsNotNone(calculation, data)
        self.assertEqual(calculation["occurrences"], 3, data)
        self.assertEqual(calculation["frequency"], "重复", data)


if __name__ == "__main__":
    unittest.main()
