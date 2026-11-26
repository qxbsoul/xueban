"""评分点链接统一校验与知识点来源可信度的回归测试数据。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

t.init()
analysis.ensure_model()
BATCH = "batch_k"
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_k", "知识点1"))
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i02", "I02", "topic_k", "知识点2"))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "k.csv", "committed", "{}", "[]", "confirmed", "2026-01-01T00:00:00"))
db.x("INSERT INTO scoring_points (id,code,batch_id,name,created_at) VALUES (?,?,?,?,?)", ("sp1", "Q001P1", BATCH, "评分点1", "2026-01-01T00:00:00"))
db.x("INSERT INTO scoring_point_knowledge_links (id,scoring_point_id,knowledge_point_id,weight,source,confidence,status,batch_id,confirmed_by,confirmed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("link1", "sp1", "kp:I01", 1.0, "teacher", 1.0, "confirmed", BATCH, "teacher", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
db.x("INSERT INTO teacher_mapping_versions (batch_id,version,updated_at,updated_by) VALUES (?,?,?,?)", (BATCH, 4, "2026-01-01T00:00:00", "teacher"))
source = t.build_source_steps({"solution_process": "2x+3=7\nx=2"})
QUESTION = {"question_type": "解答题", "max_score": 3, "scoring_points": "Q001P1 3分"}
PARSED_QUESTION = {"question_type": "解答题", "max_score": 3, "scoring_points": "按步骤给分"}


def result_with(step_links, op_links, score=3, ops=None):
    operations = ops if ops is not None else [{"operation_id": "op1", "source_step_id": "src-1", "student_fragment": "2x+3=7", "operation_type": "计算", "is_correct": True, "score": score, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": op_links}]
    return {"score": score, "max_score": 3, "is_correct": "correct", "needs_review": False,
            "step_results": [{"step_index": 1, "reasoning_step_id": "rs-1", "source_step_id": "src-1", "student_step": "2x+3=7", "is_correct": True, "score": score, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": step_links, "operation_units": operations}]}


def run_case(name, step_links, op_links, question=QUESTION, score=3, ops=None):
    try:
        out = t.validate_llm_result(result_with(step_links, op_links, score, ops), 3, question, source, BATCH)
        return {"name": name, "error": None, "review": out["needs_review"], "issues": [item["code"] for item in out["validation_issues"]], "points": out["scoring_points"], "unaddressed": out["unaddressed_scoring_points"]}
    except ValueError as exc:
        return {"name": name, "error": str(exc)}


cases = {
    "op_max_mismatch": run_case("op_max_mismatch", [], [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 5}]),
    "op_over_original": run_case("op_over_original", [], [{"scoring_point_id": "Q001P1", "awarded_score": 4, "max_score": 3}]),
    "unparsable_unknown_op": run_case("unparsable_unknown_op", [], [{"scoring_point_id": "Q999P9", "awarded_score": 1, "max_score": 1}], question=PARSED_QUESTION, score=1),
    "multi_ops_same_point": run_case("multi_ops_same_point", [], None, score=3, ops=[
        {"operation_id": "op1", "source_step_id": "src-1", "student_fragment": "a", "operation_type": "计算", "is_correct": True, "score": 1, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 3}]},
        {"operation_id": "op2", "source_step_id": "src-1", "student_fragment": "b", "operation_type": "计算", "is_correct": True, "score": 2, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}]},
    ]),
    "both_layers": run_case("both_layers", [{"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 3}], [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}]),
    "partial_ops": run_case("partial_ops", [], None, score=3, ops=[
        {"operation_id": "op1", "source_step_id": "src-1", "student_fragment": "a", "operation_type": "计算", "is_correct": True, "score": 2, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}]},
        {"operation_id": "op2", "source_step_id": "src-1", "student_fragment": "b", "operation_type": "计算", "is_correct": True, "score": 1, "max_score": 3, "error_type": "", "judgment_reason": ""},
    ]),
    "point_mismatch_total": run_case("point_mismatch_total", [], [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 3}]),
    "closed_multi_points": run_case("closed_multi_points", [], [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}], question={"question_type": "解答题", "max_score": 3, "scoring_points": "Q001P1 2分 Q001P2 1分"}, score=3, ops=[
        {"operation_id": "op1", "source_step_id": "src-1", "student_fragment": "a", "operation_type": "计算", "is_correct": True, "score": 3, "max_score": 3, "error_type": "", "judgment_reason": "", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 2, "max_score": 2}, {"scoring_point_id": "Q001P2", "awarded_score": 1, "max_score": 1}]},
    ]),
}

# 知识点来源
def knowledge_case(links):
    result = result_with([{"scoring_point_id": "Q001P1", "awarded_score": 3, "max_score": 3}], [{"scoring_point_id": "Q001P1", "awarded_score": 3, "max_score": 3}])
    result["step_results"][0]["knowledge_links"] = links
    out = t.validate_llm_result(result, 3, QUESTION, source, BATCH)
    return {"knowledge": out["step_results"][0]["knowledge_links"], "issues": [item["code"] for item in out["validation_issues"]]}

knowledge = {
    "forced_pending": knowledge_case([{"knowledge_point_id": "I01", "source_type": "model_inferred", "confirmation_status": "teacher_confirmed"}]),
    "fake_existing": knowledge_case([{"knowledge_point_id": "I02", "source_type": "existing_mapping", "confirmation_status": "confirmed"}]),
    "fake_version": knowledge_case([{"knowledge_point_id": "I01", "source_type": "existing_mapping", "mapping_version": "999", "confirmation_status": "confirmed"}]),
    "real_mapping": knowledge_case([{"knowledge_point_id": "I01", "source_type": "existing_mapping", "confirmation_status": "confirmed"}]),
    "unknown": knowledge_case([{"knowledge_point_id": "I99", "source_type": "model_inferred"}]),
    "aliases": knowledge_case([
        {"knowledge_point_id": "I01", "source_type": "existing_mapping", "confirmation_status": "confirmed"},
        {"knowledge_point_id": "kp:I01", "source_type": "existing_mapping", "confirmation_status": "confirmed"},
        {"knowledge_point_id": "indicator_i01", "source_type": "existing_mapping", "confirmation_status": "confirmed"},
    ]),
    "unknown_alias": knowledge_case([{"knowledge_point_id": "kp:I99", "source_type": "model_inferred"}]),
    "fake_alias": knowledge_case([{"knowledge_point_id": "kp:I02", "source_type": "existing_mapping", "confirmation_status": "confirmed"}]),
    "inferred_alias": knowledge_case([{"knowledge_point_id": "kp:I01", "source_type": "model_inferred", "confirmation_status": "teacher_confirmed"}]),
}
print(json.dumps({"cases": cases, "knowledge": knowledge}, ensure_ascii=False))
