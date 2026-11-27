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
    env["XUEBAN_ANALYSIS_RETENTION"] = "5"
    with tempfile.TemporaryDirectory() as tmp:
        env["XUEBAN_XUEBAN_DB"] = str(Path(tmp) / "isolated.sqlite")
        env["XUEBAN_HOME"] = str(Path(tmp) / "home")
        env["PYTHONPATH"] = str(ROOT)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name(name))], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise AssertionError(result.stderr + result.stdout)
        return json.loads(result.stdout.strip().splitlines()[-1])


class AnalysisPipelineTest(unittest.TestCase):
    """任务书第三节：自动触发、状态持久化、幂等、版本失效、LLM 只做解释。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_pipeline_worker.py")

    def test_auto_generates_deterministic_and_llm_tasks(self):
        data = self.payload["first"]
        self.assertEqual(data["deterministic_status"], "completed", data)
        self.assertEqual(data["llm_status"], "partial", data)
        self.assertIn("DEEPSEEK_API_KEY", data["llm_error"], data)
        self.assertGreaterEqual(data["tasks"], 3, data)

    def test_deterministic_result_contains_statistics(self):
        data = self.payload["detail"]
        self.assertEqual(data["classes"], 1, data)
        summary = data["question_summary"]
        self.assertEqual(summary["questions"], 1, summary)
        self.assertEqual(summary["answered"], 2, summary)
        self.assertTrue(summary["partition_ok"], summary)
        self.assertTrue(data["version"], data)

    def test_same_data_version_does_not_duplicate_tasks(self):
        data = self.payload["idempotent"]
        self.assertTrue(data["same"], data)

    def test_grading_change_invalidates_and_regenerates(self):
        data = self.payload["invalidation"]
        self.assertTrue(data["version_changed"], data)
        # 判题结果变化后，旧版本的所有任务都标记 stale
        self.assertEqual(set(data["stale_flags"]), {True}, data)
        self.assertGreater(data["after_third"], self.payload["idempotent"]["after_second"], data)

    def test_llm_layer_returns_citations_not_recomputed_numbers(self):
        data = self.payload["llm"]
        self.assertEqual(data["status"], "completed", data)
        observation = data["observations"][0]
        self.assertIn("kp:I01", observation["evidence_refs"], data)
        self.assertTrue(data["digest_has_numbers"], data)


if __name__ == "__main__":
    unittest.main()
