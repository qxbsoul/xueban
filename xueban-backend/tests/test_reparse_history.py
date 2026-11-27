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


class ReparseHistoryTest(unittest.TestCase):
    """任务书第四节：历史 raw 重解析生成新版本、旧记录保留、幂等、失败不伪造。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_reparse_worker.py")

    def test_dry_run_predicts_without_writing(self):
        self.assertEqual(self.payload["dry"]["candidates"], 2, self.payload["dry"])
        self.assertEqual(self.payload["dry"]["reparsed"], 1, self.payload["dry"])
        self.assertEqual(self.payload["dry"]["failed"], 0, self.payload["dry"])
        self.assertEqual(self.payload["dry"]["needs_review"], 1, self.payload["dry"])

    def test_real_run_creates_new_version_and_keeps_old(self):
        self.assertEqual(self.payload["real"]["reparsed"], 1, self.payload["real"])
        self.assertEqual(self.payload["real"]["failed"], 0, self.payload["real"])
        self.assertEqual(self.payload["real"]["needs_review"], 1, self.payload["real"])
        new = self.payload["new_grade"]
        self.assertEqual(new["version"], 2, new)
        self.assertEqual(new["status"], "graded", new)
        self.assertEqual(new["steps"], 1, new)
        self.assertEqual(new["reparsed_from"], "grade_old_ans_a", new)
        old = self.payload["old_grade_intact"]
        self.assertEqual(old["status"], "needs_review", old)
        self.assertEqual(old["steps"], "[]", old)

    def test_reparse_is_idempotent(self):
        self.assertEqual(self.payload["again"]["candidates"], 0, self.payload["again"])
        self.assertEqual(self.payload["again"]["remaining_candidates"], 0, self.payload["again"])

    def test_partial_credit_becomes_evidence_not_a_hundred_percent(self):
        evidence = self.payload["evidence"]
        self.assertGreaterEqual(evidence["rows"], 2, evidence)
        ratios = dict(evidence["ratios"])
        self.assertAlmostEqual(ratios["Q001P1"], 1.0, places=3, msg=evidence)
        self.assertAlmostEqual(ratios["Q001P2"], 0.0, places=3, msg=evidence)

    def test_evidence_trace_reaches_evidence_rows(self):
        trace = self.payload["trace"]
        self.assertGreaterEqual(trace["evidence_rows"], 2, trace)
        self.assertGreaterEqual(trace["steps_present"], 1, trace)

    def test_single_question_stays_insufficient_instead_of_zero(self):
        data = self.payload["class"]
        self.assertEqual(data["students"], 1, data)
        # 只有一道题、两个评分点：证据权重 1.0 < 门槛 2，必须判为「证据不足」，且不能把估计写成 0。
        self.assertEqual(data["valid"], 0, data)
        mastery = data["student_mastery"]
        self.assertTrue(mastery, data)
        for item in mastery:
            self.assertEqual(item["status"], "证据不足", item)
            self.assertIsNotNone(item["estimate"], item)
            self.assertGreater(item["estimate"], 0.0, item)
        self.assertGreaterEqual(data["student_evidence"], 2, data)


if __name__ == "__main__":
    unittest.main()
