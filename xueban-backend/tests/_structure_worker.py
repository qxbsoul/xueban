"""三层步骤结构、证据链、评分点闭合校验的隔离测试。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

t.init()
analysis.ensure_model()
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_x", "知识点1"))
source = t.build_source_steps({"solution_process": "1. 2x+3=7；移项得2x=4\n2. x=2"})
QUESTION = {"question_type": "解答题", "max_score": 5, "scoring_points": "Q001P1 3分 Q001P2 2分"}


def base_result():
    return {
        "score": 5, "max_score": 5, "is_correct": "correct", "needs_review": False,
        "step_results": [
            {"step_index": 1, "reasoning_step_id": "rs-1", "source_step_id": "src-1", "student_step": "2x+3=7；移项得2x=4", "is_correct": True, "score": 3, "max_score": 3, "error_type": "", "judgment_reason": "移项正确", "evidence_text": "2x+3=7；移项得2x=4",
             "knowledge_links": [{"knowledge_point_id": "I01", "source_type": "model_inferred"}],
             "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 3, "max_score": 3}],
             "operation_units": [
                 {"operation_id": "op-1-1", "source_step_id": "src-1", "student_fragment": "2x+3=7", "operation_type": "列式", "is_correct": True, "score": 1, "max_score": 1, "error_type": "", "judgment_reason": "正确"},
                 {"operation_id": "op-1-2", "source_step_id": "src-1", "student_fragment": "移项得2x=4", "operation_type": "移项", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确"},
             ]},
            {"step_index": 2, "reasoning_step_id": "rs-2", "source_step_id": "src-2", "student_step": "x=2", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "结果正确", "evidence_text": "x=2",
             "scoring_point_links": [{"scoring_point_id": "Q001P2", "awarded_score": 2, "max_score": 2}],
             "operation_units": [{"operation_id": "op-2-1", "source_step_id": "src-2", "student_fragment": "x=2", "operation_type": "求解", "is_correct": True, "score": 2, "max_score": 2, "error_type": "", "judgment_reason": "正确"}]},
        ],
    }


good = t.validate_llm_result(base_result(), 5, QUESTION, source, "batch_x")
unmapped = base_result()
unmapped["step_results"][0]["source_step_id"] = "src-999"
unmapped = t.validate_llm_result(unmapped, 5, QUESTION, source, "batch_x")
multi = base_result()
multi["score"] = 2
multi["step_results"] = [
    {"step_index": 1, "source_step_id": "src-1", "student_step": "2x+3=7", "is_correct": False, "score": 2, "max_score": 3, "error_type": "计算操作错误", "judgment_reason": "部分", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}], "operation_units": [{"operation_id": "op-a", "score": 2, "max_score": 3, "is_correct": False, "judgment_reason": "部分"}]},
    {"step_index": 2, "source_step_id": "src-2", "student_step": "x=2", "is_correct": False, "score": 0, "max_score": 2, "error_type": "计算操作错误", "judgment_reason": "错", "scoring_point_links": [{"scoring_point_id": "Q001P2", "awarded_score": 0, "max_score": 2}], "operation_units": [{"operation_id": "op-b", "score": 0, "max_score": 2, "is_correct": False, "judgment_reason": "错"}]},
]
multi["step_results"][1]["scoring_point_links"] = [{"scoring_point_id": "Q001P1", "awarded_score": 0, "max_score": 3}]
multi = t.validate_llm_result(multi, 5, QUESTION, source, "batch_x")

over = base_result()
over["step_results"][0]["scoring_point_links"] = [{"scoring_point_id": "Q001P1", "awarded_score": 5, "max_score": 3}]
over_error = None
try:
    t.validate_llm_result(over, 5, QUESTION, source, "batch_x")
except ValueError as exc:
    over_error = str(exc)

unknown = base_result()
unknown["step_results"][0]["scoring_point_links"] = [{"scoring_point_id": "Q999P9", "awarded_score": 1, "max_score": 1}]
unknown_error = None
try:
    t.validate_llm_result(unknown, 5, QUESTION, source, "batch_x")
except ValueError as exc:
    unknown_error = str(exc)

op_mismatch = base_result()
op_mismatch["step_results"][0]["operation_units"][0]["score"] = 0
op_mismatch = t.validate_llm_result(op_mismatch, 5, QUESTION, source, "batch_x")

unaddressed = base_result()
unaddressed["step_results"] = [unaddressed["step_results"][1]]
unaddressed["score"] = 2
unaddressed = t.validate_llm_result(unaddressed, 5, QUESTION, source, "batch_x")

print(json.dumps({
    "source": source,
    "good": {"review": good["needs_review"], "validation": good["validation"], "source_ids": [step["source_step_id"] for step in good["step_results"]], "knowledge": good["step_results"][0]["knowledge_links"], "scoring": good["scoring_points"]},
    "unmapped": {"review": unmapped["needs_review"], "source": unmapped["step_results"][0]["source_step_id"], "issues": [item["code"] for item in unmapped["validation_issues"]]},
    "multi": {"review": multi["needs_review"], "point": multi["scoring_points"].get("Q001P1"), "issues": [item["code"] for item in multi["validation_issues"]]},
    "over_error": over_error,
    "unknown_error": unknown_error,
    "op_mismatch": {"review": op_mismatch["needs_review"], "issues": [item["code"] for item in op_mismatch["validation_issues"]]},
    "unaddressed": {"review": unaddressed["needs_review"], "missing": unaddressed["unaddressed_scoring_points"], "issues": [item["code"] for item in unaddressed["validation_issues"]]},
}, ensure_ascii=False))
