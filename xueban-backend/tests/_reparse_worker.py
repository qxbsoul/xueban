"""历史 raw_response 重解析：生成新版本、保留旧记录、幂等、失败不伪造。"""
import json

from xueban.api import xueban_teacher as t
from xueban.services import learning_analysis as analysis
from xueban.services import xueban_store as db

t.init()
NOW = "2026-01-01T00:00:00"
BATCH = "batch_p"
db.x("INSERT INTO teacher_indicators (id,code,topic_id,description) VALUES (?,?,?,?)", ("indicator_i01", "I01", "topic_x", "知识点1"))
db.x("INSERT INTO teacher_topics (id,code,title,created_at) VALUES (?,?,?,?)", ("topic_x", "T01", "主题", NOW))
db.x("INSERT INTO teacher_batches (id,filename,status,counts_json,errors_json,preprocess_status,created_at) VALUES (?,?,?,?,?,?,?)", (BATCH, "p.csv", "committed", "{}", "[]", "confirmed", NOW))
db.x("INSERT INTO teacher_classes (id,code,name,grade,created_at) VALUES (?,?,?,?,?)", ("class_p", "PC", "测试班", 7, NOW))
db.x("INSERT INTO teacher_students (id,user_id,code,class_id,name,grade,created_at) VALUES (?,?,?,?,?,?,?)", ("stu_p", "user_p", "P01", "class_p", "P01", 7, NOW))
db.x("INSERT INTO teacher_questions (id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type) VALUES (?,?,?,?,?,?,?,?)", ("Q001", "topic_x", "1+1=?", "2", 2, "Q001P1 1分 Q001P2 1分", "I01", "解答题"))
db.x("INSERT INTO teacher_question_versions (version_id,batch_id,question_id,topic_id,stem,reference_answer,max_score,scoring_points,indicator_id,question_type,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"qv:{BATCH}:Q001", BATCH, "Q001", "topic_x", "1+1=?", "2", 2, "Q001P1 1分 Q001P2 1分", "I01", "解答题", NOW))
for index, suffix in enumerate(("a", "b")):
    db.x(
        "INSERT INTO teacher_answers (id,student_id,question_id,event_id,revision,grading_status,import_batch_id,max_score,original_score,solution_process,original_answer,question_version_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"ans_{suffix}", "stu_p", "Q001", f"E{index}", 1, "needs_review", BATCH, 2, 1, "1. 第一步", "2", f"qv:{BATCH}:Q001"),
    )
analysis.ensure_model()

RAW_OK = {
    "score": 1, "max_score": 2, "is_correct": "partial", "needs_review": True,
    "reasoning_steps": [{
        "step_index": 1, "source_step_id": "src-1", "student_step": "1. 第一步",
        "normalized_step": "第一步", "operation_type": "列式", "reasoning_purpose": "", "expected_step": "",
        "is_correct": False, "score": 1, "max_score": 2, "error_type": "计算操作错误", "judgment_reason": "部分正确",
        "evidence_text": "1. 第一步",
        "scoring_point_links": [
            {"scoring_point_id": "Q001P1", "awarded_score": 1, "max_score": 1},
            {"scoring_point_id": "Q001P2", "awarded_score": 0, "max_score": 1},
        ],
        "operation_units": [
            {"operation_id": "op-1", "source_step_id": "src-1", "student_fragment": "第一步前半", "is_correct": True, "score": 0.5, "max_score": 1, "error_type": "", "judgment_reason": "对一半", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 0.5, "max_score": 0.5}]},
            {"operation_id": "op-2", "source_step_id": "src-1", "student_fragment": "第一步后半", "is_correct": False, "score": 0.5, "max_score": 1, "error_type": "计算操作错误", "judgment_reason": "其余错误", "scoring_point_links": [{"scoring_point_id": "Q001P1", "awarded_score": 0.5, "max_score": 0.5}]},
        ],
    }],
}
RAW_BAD = json.loads(json.dumps(RAW_OK))
RAW_BAD["reasoning_steps"][0]["scoring_point_links"][0]["awarded_score"] = 9

for answer_id, raw in (("ans_a", RAW_OK), ("ans_b", RAW_BAD)):
    db.x("INSERT INTO teacher_grade_runs (id,answer_id,student_id,question_id,event_id,revision,grading_version,grading_model,grading_prompt_version,grader_version,score,max_score,is_correct,step_results_json,needs_review,status,raw_response,created_at,question_version_id,structure_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (f"grade_old_{answer_id}", answer_id, "stu_p", "Q001", f"E{0 if answer_id == 'ans_a' else 1}", 1, 1, "deepseek-chat", "teacher-grade-v4", "v1.5.13", 1, 2, "partial", "[]", 1, "needs_review", json.dumps(raw, ensure_ascii=False), NOW, f"qv:{BATCH}:Q001", "{}"))

dry = t.reparse_history(BATCH, dry_run=True)
real = t.reparse_history(BATCH, dry_run=False)
again = t.reparse_history(BATCH, dry_run=False)
trace = analysis.evidence_trace(BATCH)
profiles = analysis.class_profile(BATCH, "class_p")
new_grade = db.q1("SELECT * FROM teacher_grade_runs WHERE answer_id='ans_a' ORDER BY grading_version DESC LIMIT 1")
old_grade = db.q1("SELECT * FROM teacher_grade_runs WHERE id='grade_old_ans_a'")
evidence = analysis._evidence(BATCH)[0]

print(json.dumps({
    "dry": {k: dry[k] for k in ("candidates", "reparsed", "needs_review", "failed")},
    "real": {k: real[k] for k in ("candidates", "reparsed", "needs_review", "failed", "remaining_candidates")},
    "again": {k: again[k] for k in ("candidates", "reparsed", "failed", "remaining_candidates")},
    "new_grade": {"version": new_grade["grading_version"], "status": new_grade["status"], "steps": len(json.loads(new_grade["step_results_json"])), "reparsed_from": json.loads(new_grade["request_context_json"]).get("reparsed_from")},
    "old_grade_intact": {"status": old_grade["status"], "steps": old_grade["step_results_json"]},
    "evidence": {"rows": len(evidence), "points": sorted({item["scoring_point_id"] for item in evidence}), "ratios": sorted({(item["scoring_point_id"], item["score_ratio"]) for item in evidence})},
    "trace": {stage["stage"]: stage["count"] for stage in trace["stages"]},
    "class": {"students": profiles.get("student_count"), "valid": profiles.get("valid_student_count"), "kps": [(item["name"], item["mean_mastery"]) for item in profiles.get("knowledge_points") or []],
              "student_mastery": [{ "name": item["knowledge_point_name"], "status": item["status"], "estimate": item["mastery_estimate"], "evidence": item["evidence_count"], "weight": item["effective_evidence_weight"]} for item in (profiles.get("students") or [{}])[0].get("mastery", [])],
              "student_evidence": ((profiles.get("students") or [{}])[0].get("data_quality") or {}).get("evidence_count")},
}, ensure_ascii=False))
