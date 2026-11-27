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


class EvidenceChainTest(unittest.TestCase):
    """任务书第四节：未映射不连坐、人工修正证据、多任务重新估计、逐级追溯。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_evidence_chain_worker.py")

    def test_unmapped_sibling_does_not_dilute_mapped_point(self):
        data = self.payload["unmapped_sibling"]
        self.assertEqual(data["points"], ["Q001P1"], data)
        self.assertAlmostEqual(data["weights"][0], 1.0, places=4, msg=data)
        self.assertAlmostEqual(data["base"][0], 1.0, places=4, msg=data)

    def test_trace_reports_unmapped_point(self):
        self.assertEqual(self.payload["trace_a"]["excluded"].get("unmapped_points", 0), 1, self.payload["trace_a"])

    def test_manual_correction_without_point_confirmation_yields_no_evidence(self):
        self.assertEqual(self.payload["manual_before"]["rows"], 0, self.payload["manual_before"])
        self.assertEqual(self.payload["trace_a"]["excluded"].get("manual_corrected_without_point_confirmation", 0), 0, self.payload["trace_a"])

    def test_confirmed_point_restores_evidence_with_teacher_source(self):
        data = self.payload["manual_after"]
        self.assertEqual(data["rows"], 1, data)
        self.assertEqual(data["source"], ["teacher_confirmed"], data)
        self.assertAlmostEqual(data["ratio"][0], 1.0, places=4, msg=data)

    def test_multi_task_reestimates_from_merged_evidence(self):
        data = self.payload["multi"]
        self.assertEqual(data["batches"], 2, data)
        self.assertTrue(data["mastery"], data)
        point = data["mastery"][0]
        # 两个任务各一条证据：权重 2.0、覆盖 2 道题，达到门槛后可给出结论；
        # 单任务画像权重只有 1.0，仍是「证据不足」——说明不是对两次掌握度取平均。
        self.assertAlmostEqual(point["weight"], 2.0, places=4, msg=point)
        self.assertEqual(point["covered"], 2, point)
        self.assertEqual(point["opportunity"], 2, point)
        self.assertEqual(point["rows"], 2, point)
        self.assertNotEqual(point["status"], "证据不足", point)
        self.assertEqual(self.payload["profile_a"]["mastery"][0]["status"], "证据不足", self.payload["profile_a"])
        self.assertAlmostEqual(self.payload["profile_a"]["mastery"][0]["weight"], 1.0, places=4, msg=self.payload["profile_a"])


if __name__ == "__main__":
    unittest.main()
