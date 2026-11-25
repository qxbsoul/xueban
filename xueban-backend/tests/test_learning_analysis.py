import math
import unittest

from xueban.services import learning_analysis as la


class BetaEvidenceTest(unittest.TestCase):
    def test_manual_beta_update(self):
        # Two scoring points, equal weight, ratios 1 and 0.5.
        # alpha = 2 + 1*1 + 1*0.5 = 3.5
        # beta  = 2 + 0 + 0.5 = 2.5
        # mastery = 3.5 / 6 = 0.583333...
        evidence = [
            {"knowledge_point_id": "kp:test", "knowledge_point_name": "测试知识点", "topic": "测试", "student_id": "s1", "student_code": "S1", "class_id": "c1", "class_code": "C1", "answer_id": "a1", "question_id": "q1", "scoring_point_id": "P1", "earned_score": 1, "full_score": 1, "score_ratio": 1.0, "weight": 1.0, "error_type": "无法确定", "error_reason": "", "evidence_text": "x", "judgment_source": "ai", "confidence": 1.0},
            {"knowledge_point_id": "kp:test", "knowledge_point_name": "测试知识点", "topic": "测试", "student_id": "s1", "student_code": "S1", "class_id": "c1", "class_code": "C1", "answer_id": "a2", "question_id": "q2", "scoring_point_id": "P2", "earned_score": 1, "full_score": 2, "score_ratio": 0.5, "weight": 1.0, "error_type": "计算操作错误", "error_reason": "test", "evidence_text": "y", "judgment_source": "ai", "confidence": 1.0},
        ]
        weight = sum(item["weight"] for item in evidence)
        alpha = la.A0 + sum(item["weight"] * item["score_ratio"] for item in evidence)
        beta = la.B0 + sum(item["weight"] * (1 - item["score_ratio"]) for item in evidence)
        self.assertAlmostEqual(alpha, 3.5)
        self.assertAlmostEqual(beta, 2.5)
        self.assertAlmostEqual(alpha / (alpha + beta), 3.5 / 6.0)
        low, high = la._interval(alpha, beta)
        self.assertLess(low, alpha / (alpha + beta))
        self.assertGreater(high, alpha / (alpha + beta))
        self.assertEqual(weight, 2.0)

    def test_status_prioritises_insufficient_evidence(self):
        self.assertEqual(la._status(0.1, 0.0, 1.0), "证据不足")
        self.assertEqual(la._status(0.1, 5.0, 0.5), "掌握不足")
        self.assertEqual(la._status(0.9, 5.0, 0.5), "掌握程度较高")

    def test_error_canonical(self):
        self.assertEqual(la._canonical_error("合并同类项错误"), "其他")
        self.assertEqual(la._canonical_error("计算操作错误"), "计算操作错误")
        self.assertEqual(la._canonical_error(""), "无法确定")


if __name__ == "__main__":
    unittest.main()
