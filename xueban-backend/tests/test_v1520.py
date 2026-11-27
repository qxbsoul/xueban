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


class V1520Test(unittest.TestCase):
    """v1.5.20：预处理状态互斥/记录下钻、证据分页与错误分类、确定性缓存。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_v1520_worker.py")

    def test_preprocess_states_mutually_exclusive(self):
        report = self.payload["report"]
        self.assertEqual(report["raw_records"], 5, report)
        self.assertEqual(report["state_sum"], report["raw_records"], report)
        self.assertTrue(report["reconciled"], report)
        self.assertEqual(report["valid_records"] + report["anomaly_records"] + report["pending_records"], 5, report)
        self.assertEqual(report["process_reminders"], 1, report)

    def test_anomaly_records_and_issue_records_are_different_units(self):
        report = self.payload["report"]
        self.assertEqual(report["anomaly_records"], 1, report)
        self.assertEqual(report["issue_records_distinct"], 5, report)
        self.assertNotEqual(report["anomaly_records"], report["pending_issues"], report)
        self.assertIn("anomaly_records", self.payload["units"])
        self.assertIn("issue_total", self.payload["units"])

    def test_anomaly_record_drilldown_has_all_issues(self):
        data = self.payload["anomaly"]
        self.assertEqual(data["total"], 1, data)
        record = data["records"][0]
        self.assertEqual(record["q"], "Q105", data)
        self.assertGreaterEqual(record["issue_count"], 1, data)
        self.assertIn("scoring_points_sum_mismatch", record["types"], data)
        self.assertEqual(record["statuses"], ["pending_review"], data)

    def test_pending_and_valid_process_rules(self):
        pending = self.payload["pending"]
        valid = self.payload["valid"]
        self.assertEqual(pending["total"], 2, pending)
        self.assertIn("Q103", pending["questions"], pending)
        self.assertIn("Q104", pending["questions"], pending)
        self.assertEqual(valid["total"], 2, valid)
        self.assertIn("Q101", valid["questions"], valid)
        self.assertIn("Q102", valid["questions"], valid)
        # 只需答案的选择题缺过程不能进入需人工核实。
        self.assertNotIn("Q101", pending["questions"], pending)

    def test_issue_status_filter(self):
        self.assertEqual(self.payload["issue_filter_total"], 2, self.payload)

    def test_evidence_pagination_and_filters(self):
        evidence = self.payload["evidence"]
        self.assertEqual(evidence["total"], 60, evidence)
        self.assertEqual(evidence["page1"], 20, evidence)
        self.assertEqual(evidence["page3"], 20, evidence)
        self.assertEqual(evidence["correct_total"], 30, evidence)

    def test_correct_and_unidentified_errors(self):
        labels = self.payload["evidence"]["labels"]
        self.assertIn("正确", labels, labels)
        self.assertIn("未识别错误", labels, labels)
        self.assertNotIn("无法确定", labels, labels)

    def test_deterministic_cache_reused(self):
        cache = self.payload["cache"]
        self.assertTrue(cache["same_tasks"], cache)
        self.assertEqual(cache["task_count"], 1, cache)
        self.assertTrue(cache["has_classes"], cache)


if __name__ == "__main__":
    unittest.main()
