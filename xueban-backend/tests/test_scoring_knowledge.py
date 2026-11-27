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


class ScoringLinkValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_scoring_knowledge_worker.py")
        cls.cases = cls.payload["cases"]

    def test_operation_max_must_equal_original(self):
        # v1.5.17：模型声明满分高于原始满分、但给分未超原始满分时，修回原始满分并进入复核；
        # 只有给分本身超过原始满分才判失败（见 op_over_original）。
        case = self.cases["op_max_mismatch"]
        self.assertIsNone(case["error"], case)
        self.assertTrue(case["review"], case)
        self.assertIn("scoring_point_declared_max_clamped", case["issues"], case)
        self.assertEqual(case["points"]["Q001P1"]["max"], 3.0, case)

    def test_operation_award_is_capped_and_reviewed(self):
        case = self.cases["op_over_original"]
        self.assertIsNone(case["error"], case)
        self.assertFalse(case["review"], case)
        self.assertIn("scoring_point_awarded_capped", case["issues"], case)
        self.assertEqual(case["points"]["Q001P1"]["awarded"], 3.0, case)

    def test_unparsable_scoring_points_cannot_bypass_unknown_id(self):
        case = self.cases["unparsable_unknown_op"]
        self.assertIsNotNone(case["error"], case)

    def test_same_point_multiple_operations_accumulate_once(self):
        case = self.cases["multi_ops_same_point"]
        self.assertIsNone(case["error"], case)
        self.assertEqual(case["points"]["Q001P1"]["awarded"], 3.0, case)
        self.assertEqual(case["points"]["Q001P1"]["evidence_count"], 2, case)

    def test_step_and_operation_links_have_single_source(self):
        case = self.cases["both_layers"]
        self.assertIsNone(case["error"], case)
        self.assertEqual(case["points"]["Q001P1"]["awarded"], 2.0, case)
        self.assertIn("step_links_ignored", case["issues"], case)

    def test_partial_operation_links_are_reviewed(self):
        case = self.cases["partial_ops"]
        self.assertTrue(case["review"], case)
        self.assertIn("operation_without_scoring_point", case["issues"], case)

    def test_point_total_mismatch_is_reviewed(self):
        case = self.cases["point_mismatch_total"]
        self.assertTrue(case["review"], case)
        self.assertIn("scoring_points_do_not_close", case["issues"], case)

    def test_closed_multi_points_pass(self):
        case = self.cases["closed_multi_points"]
        self.assertIsNone(case["error"], case)
        self.assertFalse(case["review"], case)
        self.assertEqual(case["unaddressed"], [], case)


class KnowledgeSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = run_worker("_scoring_knowledge_worker.py")
        cls.knowledge = cls.payload["knowledge"]

    def test_model_inferred_cannot_claim_teacher_confirmed(self):
        link = self.knowledge["forced_pending"]["knowledge"][0]
        self.assertEqual(link["source_type"], "model_inferred", link)
        self.assertEqual(link["confirmation_status"], "pending", link)

    def test_fake_existing_mapping_is_downgraded(self):
        link = self.knowledge["fake_existing"]["knowledge"][0]
        self.assertEqual(link["confirmation_status"], "pending", link)
        self.assertEqual(link["source_type"], "model_inferred", link)

    def test_model_mapping_version_is_ignored(self):
        link = self.knowledge["fake_version"]["knowledge"][0]
        self.assertEqual(link["mapping_version"], 4, link)

    def test_real_confirmed_mapping_is_preserved(self):
        link = self.knowledge["real_mapping"]["knowledge"][0]
        self.assertEqual(link["confirmation_status"], "confirmed", link)
        self.assertEqual(link["source_type"], "existing_mapping", link)

    def test_unknown_knowledge_point_is_kept(self):
        link = self.knowledge["unknown"]["knowledge"][0]
        self.assertEqual(link["knowledge_point_id"], "I99", link)
        self.assertIn("unknown_knowledge_point", self.knowledge["unknown"]["issues"], self.knowledge)

    def test_legal_aliases_match_same_confirmed_mapping(self):
        links = self.knowledge["aliases"]["knowledge"]
        self.assertEqual(len(links), 3, self.knowledge["aliases"])
        for link in links:
            self.assertEqual(link["knowledge_point_id"], "I01", link)
            self.assertEqual(link["confirmation_status"], "confirmed", link)
            self.assertEqual(link["source_type"], "existing_mapping", link)
            self.assertEqual(link["mapping_link_id"], "link1", link)
        self.assertNotIn("unknown_knowledge_point", self.knowledge["aliases"]["issues"], self.knowledge["aliases"])

    def test_unknown_alias_is_reported(self):
        link = self.knowledge["unknown_alias"]["knowledge"][0]
        self.assertEqual(link["knowledge_point_id"], "kp:I99", link)
        self.assertIn("unknown_knowledge_point", self.knowledge["unknown_alias"]["issues"], self.knowledge)

    def test_fake_alias_confirmation_is_downgraded(self):
        link = self.knowledge["fake_alias"]["knowledge"][0]
        self.assertEqual(link["confirmation_status"], "pending", link)
        self.assertEqual(link["source_type"], "model_inferred", link)

    def test_alias_does_not_upgrade_model_inference(self):
        link = self.knowledge["inferred_alias"]["knowledge"][0]
        self.assertEqual(link["knowledge_point_id"], "I01", link)
        self.assertEqual(link["confirmation_status"], "pending", link)
        self.assertEqual(link["source_type"], "model_inferred", link)


if __name__ == "__main__":
    unittest.main()
