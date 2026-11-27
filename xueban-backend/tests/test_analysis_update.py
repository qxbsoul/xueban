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


class AnalysisUpdateTest(unittest.TestCase):
    """v1.5.17：判题变化自动更新学情；对象级版本、去重、失败不覆盖、人工修正保留 AI 历史。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_analysis_update_worker.py")

    def test_event_creates_object_tasks_with_versions(self):
        data = self.payload["first"]
        self.assertEqual(data["events"], 1, data)
        self.assertEqual(data["tasks"], 2, data)
        self.assertEqual(data["student_status"], "completed", data)
        for key in ("scope", "object_id", "evidence_version", "prompt_version", "model", "llm"):
            self.assertIn(key, data["result_keys"], data)
        self.assertTrue(data["evidence_version"], data)
        self.assertEqual(data["prompt_version"], "analysis-llm-v2", data)

    def test_duplicate_event_does_not_duplicate_tasks(self):
        data = self.payload["duplicate"]
        self.assertEqual(data["llm_tasks"], 2, data)
        self.assertEqual(data["events"], 1, data)

    def test_grade_change_regenerates_and_supersedes(self):
        data = self.payload["changed"]
        self.assertEqual(data["new_tasks"], 2, data)
        self.assertEqual(data["total_llm_tasks"], 4, data)
        self.assertEqual(data["old_superseded"], 1, data)
        self.assertEqual(data["latest_status"], "completed", data)
        self.assertTrue(data["mastery_v1"] or data["mastery_v2"], self.payload["debug_evidence"])
        self.assertNotEqual(data["mastery_v1"], data["mastery_v2"], data)

    def test_llm_failure_does_not_overwrite_valid_result(self):
        data = self.payload["failure"]
        self.assertTrue(data["latest_still_valid"], data)
        self.assertGreaterEqual(data["failed_count"], 1, data)
        self.assertEqual(data["failed_status"], "partial", data)

    def test_manual_correction_keeps_ai_history_and_updates_stats(self):
        data = self.payload["manual"]
        self.assertEqual(data["ai_score_preserved"], 1, data)
        self.assertTrue(data["ai_raw_preserved"], data)
        self.assertEqual(data["row_after"]["status"], "manual_corrected", data)
        self.assertEqual(data["row_after"]["score"], 0.5, data)
        self.assertIsNotNone(data["knowledge_mastery"], self.payload["debug_evidence"])
        self.assertEqual(data["grading_version"], 3, data)


if __name__ == "__main__":
    unittest.main()
