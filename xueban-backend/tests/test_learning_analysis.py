import math
import unittest

from xueban.services import learning_analysis as la
from xueban.api.xueban_teacher import validate_llm_result


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
        self.assertLess(abs(la._interval(2, 2)[0] - 0.0943), 0.002)
        self.assertLess(abs(la._interval(2, 2)[1] - 0.9057), 0.002)
        self.assertLess(low, alpha / (alpha + beta))
        self.assertGreater(high, alpha / (alpha + beta))
        self.assertEqual(weight, 2.0)

    def test_status_prioritises_insufficient_evidence(self):
        self.assertEqual(la._status(0.1, 0.0, 1.0, 0), "证据不足")
        self.assertEqual(la._status(0.1, 5.0, 0.5, 3), "掌握不足")
        self.assertEqual(la._status(0.9, 5.0, 0.5, 3), "熟练掌握")
        self.assertEqual(la._status(0.7, 5.0, 0.5, 3), "基本掌握")
        self.assertEqual(la._status(0.55, 5.0, 0.5, 3), "初步掌握")
        self.assertEqual(la._status(0.9, 5.0, 0.1, 3), "证据不足")
        self.assertEqual(la._status(0.9, la.MIN_EVIDENCE - 0.5, 0.9, 3), "证据不足")
        self.assertEqual(la._status(0.9, 5.0, 0.9, la.MIN_QUESTIONS - 1), "证据不足")

    def test_error_canonical(self):
        # v1.5.20：统一六类，未知标签进入“未识别错误”，不再出现“其他/无法确定”。
        self.assertEqual(la._canonical_error("合并同类项错误"), "未识别错误")
        self.assertEqual(la._canonical_error("计算操作错误"), "计算操作错误")
        self.assertEqual(la._canonical_error("公式使用错误"), "方法选择错误")
        self.assertEqual(la._canonical_error("步骤遗漏"), "推理过程错误")
        self.assertEqual(la._canonical_error(""), "未识别错误")
        self.assertEqual(la.ERROR_TAXONOMY, ["概念理解错误", "方法选择错误", "推理过程错误", "计算操作错误", "表达规范错误", "未识别错误"])


class ModelValidationTest(unittest.TestCase):
    question = {"question_type": "解答题", "scoring_points": "Q001P1 1分; Q001P2 1分", "max_score": 2}

    def test_unknown_scoring_point_rejected(self):
        result = {"score": 1, "step_results": [{"step_index": 1, "student_step": "x", "is_correct": True, "score": 1, "max_score": 1, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": "Q999P1", "awarded_score": 1, "max_score": 1}]}]}
        with self.assertRaises(ValueError):
            validate_llm_result(result, 2, self.question)

    def test_duplicate_scoring_point_rejected(self):
        result = {"score": 2, "step_results": [{"step_index": 1, "student_step": "x", "is_correct": True, "score": 1, "max_score": 1, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1}]}, {"step_index": 2, "student_step": "y", "is_correct": True, "score": 1, "max_score": 1, "error_type": "", "judgment_reason": "ok", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1}]}]}
        with self.assertRaises(ValueError):
            validate_llm_result(result, 2, self.question)

    def test_score_above_max_rejected(self):
        with self.assertRaises(ValueError):
            validate_llm_result({"score": 3, "step_results": []}, 2, self.question)


if __name__ == "__main__":
    unittest.main()
